import json
import os
import sys
import numpy as np
import random
import math
import time

import torch
import torch.nn as nn
from torch.autograd import Variable
from torch import optim
import torch.nn.functional as F

from env import R2RBatch
from utils import padding_idx, add_idx, Tokenizer
import utils
import model
import param
from param import args
from collections import defaultdict
import encoder
from model import ObjEncoder


class BaseAgent(object):
    ''' Base class for an R2R agent to generate and save trajectories. '''

    def __init__(self, env, results_path):
        self.env = env
        self.results_path = results_path
        random.seed(1)
        self.results = {}
        self.losses = [] # For learning agents
    
    def write_results(self):
        output = [{'instr_id':k, 'trajectory': v} for k,v in self.results.items()]
        with open(self.results_path, 'w') as f:
            json.dump(output, f)

    def get_results(self):
        output = [{'instr_id': k, 'trajectory': v} for k, v in self.results.items()]
        return output

    def rollout(self, **args):
        ''' Return a list of dicts containing instr_id:'xx', path:[(viewpointId, heading_rad, elevation_rad)]  '''
        raise NotImplementedError

    @staticmethod
    def get_agent(name):
        return globals()[name+"Agent"]

    def test(self, iters=None, **kwargs):
        self.env.reset_epoch(shuffle=(iters is not None))   # If iters is not none, shuffle the env batch
        self.losses = []
        self.results = {}
        # We rely on env showing the entire batch before repeating anything
        looped = False
        self.loss = 0
        if iters is not None:
            # For each time, it will run the first 'iters' iterations. (It was shuffled before)
            for i in range(iters):
                for traj in self.rollout(**kwargs):
                    self.loss = 0
                    self.results[traj['instr_id']] = traj['path']
        else:   # Do a full round
            while True:
                for traj in self.rollout(**kwargs):
                    if traj['instr_id'] in self.results:
                        looped = True
                    else:
                        self.loss = 0
                        self.results[traj['instr_id']] = traj['path']
                if looped:
                    break

class Seq2SeqAgent(BaseAgent):
    ''' An agent based on an LSTM seq2seq model with attention. '''

    # For now, the agent can't pick which forward move to make - just the one in the middle
    env_actions = {
      'left': (0,-1, 0), # left
      'right': (0, 1, 0), # right
      'up': (0, 0, 1), # up
      'down': (0, 0,-1), # down
      'forward': (1, 0, 0), # forward
      '<end>': (0, 0, 0), # <end>
      '<start>': (0, 0, 0), # <start>
      '<ignore>': (0, 0, 0)  # <ignore>
    }

    def __init__(self, env, results_path, tok, episode_len=20):
        super(Seq2SeqAgent, self).__init__(env, results_path)
        self.tok = tok
        self.episode_len = episode_len
        self.feature_size = self.env.feature_size

        # Models
        enc_hidden_size = args.rnn_dim//2 if args.bidir else args.rnn_dim
        # self.encoder = model.EncoderLSTM(tok.vocab_size(), args.wemb, enc_hidden_size, padding_idx,
        #                                  args.dropout, bidirectional=args.bidir).cuda()
        self.glove_dim = 300
        # with open('/VL/space/zhan1624/R2R-EnvDrop/img_features/object_vocab.txt', 'r') as f_ov:
        #     self.obj_vocab = [k.strip() for k in f_ov.readlines()]
        # glove_matrix = get_glove_matrix(self.obj_vocab, self.glove_dim)
        # self.objencoder = ObjEncoder(glove_matrix.size(0), glove_matrix.size(1), glove_matrix).cuda()
        

        encoder_kwargs = {
        'vocab_size': tok.vocab_size(),
        'embedding_size': args.word_embedding_size,
        'hidden_size': args.rnn_hidden_size,
        'padding_idx': padding_idx,
        'dropout_ratio': args.rnn_dropout,
        'bidirectional': args.bidirectional == 1,
        'num_layers': args.rnn_num_layers
    }
        self.encoder = encoder.EncoderBERT(**encoder_kwargs).cuda()
        self.decoder = model.ConfigurationDecoder(args.aemb, args.rnn_dim, args.dropout, feature_size=self.feature_size + args.angle_feat_size).cuda()
        self.critic = model.Critic().cuda()
        self.models = (self.encoder, self.decoder, self.critic)

        # Optimizers
        self.encoder_optimizer = args.optimizer(self.encoder.parameters(), lr=args.lr)
        self.decoder_optimizer = args.optimizer(self.decoder.parameters(), lr=args.lr)
        self.critic_optimizer = args.optimizer(self.critic.parameters(), lr=args.lr)
        self.optimizers = (self.encoder_optimizer, self.decoder_optimizer, self.critic_optimizer)

        # Evaluations
        self.losses = []
        self.criterion = nn.CrossEntropyLoss(ignore_index=args.ignoreid, size_average=False)

        # Logs
        sys.stdout.flush()
        self.logs = defaultdict(list)
        self.obj_feat = self._obj_feature(args.obj_img_feat_path)

        #different spatial features
        self.motion_indicator_feature = self.get_motion_indicator_feature(args.train_motion_indi_path, args.val_seen_motion_indi_path, \
                                            args.val_unseen_motion_indi_path, args.motion_indicator_aug)
        self.landmark_feature = self.get_landmark_feature(args.train_landmark_path, args.val_seen_landmark_path, \
                                                        args.val_unseen_landmark_path, args.landmark_aug)
        self.landmark_triplet_vector = self.get_landmark_triplets(args.train_landmark_triplet, args.val_seen_landmark_triplet, args.val_unseen_landmark_triplet)


    def _sort_batch(self, obs):
        ''' Extract instructions from a list of observations and sort by descending
            sequence length (to enable PyTorch packing). '''

        seq_tensor = np.array([ob['instr_encoding'] for ob in obs])
        seq_lengths = np.argmax(seq_tensor == padding_idx, axis=1)
        seq_lengths[seq_lengths == 0] = seq_tensor.shape[1]     # Full length

        seq_tensor = torch.from_numpy(seq_tensor)
        seq_lengths = torch.from_numpy(seq_lengths)

        # Sort sequences by lengths
        seq_lengths, perm_idx = seq_lengths.sort(0, True)       # True -> descending
        sorted_tensor = seq_tensor[perm_idx]
        mask = (sorted_tensor == padding_idx)[:,:seq_lengths[0]]    # seq_lengths[0] is the Maximum length

        return Variable(sorted_tensor, requires_grad=False).long().cuda(), \
               mask.bool().cuda(),  \
               list(seq_lengths), list(perm_idx)

    def _feature_variable(self, obs):
        ''' Extract precomputed features into variable. '''
        features = np.empty((len(obs), args.views, self.feature_size + args.angle_feat_size), dtype=np.float32)
        for i, ob in enumerate(obs):
            features[i, :, :] = ob['feature']   # Image feat
        return Variable(torch.from_numpy(features), requires_grad=False).cuda()
    
    def _obj_feature(self, object_img_feat_path):
        all_obj = np.load(object_img_feat_path, allow_pickle=True).item()
        for scan_key, scan_value in  all_obj.items():
            for state_key, state_value in scan_value.items():
                for heading_elevation_key, heading_elevation_value in state_value.items():
                    heading_elevation_value['text_feature'] = torch.from_numpy(heading_elevation_value['text_feature'])
                    heading_elevation_value['text_mask'] = torch.from_numpy(heading_elevation_value['text_mask'])

        return all_obj
    
    def get_motion_indicator_feature(self, train_motion_indi_dir,val_seen_motion_indi_dir,val_unseen_motion_indi_dir, aug_motion_indi_dir, test_motion_indi_dir=None):
        motion_indicator_dict = {}
        motion_indicator_dict1 = np.load(train_motion_indi_dir, allow_pickle=True).item()
        motion_indicator_dict2 = np.load(val_seen_motion_indi_dir, allow_pickle=True).item()
        motion_indicator_dict3 = np.load(val_unseen_motion_indi_dir, allow_pickle=True).item()
        #motion_indicator_dict4 = np.load(test_motion_indi_dir, allow_pickle=True).item()
        # motion_indicator_dict5 = np.load(aug_motion_indi_dir, allow_pickle=True).item()

        motion_indicator_dict.update(motion_indicator_dict1)
        motion_indicator_dict.update(motion_indicator_dict2)
        motion_indicator_dict.update(motion_indicator_dict3)
        #motion_indicator_dict.update(motion_indicator_dict4)
        # motion_indicator_dict.update(motion_indicator_dict5)
        return motion_indicator_dict
    
    def get_landmark_feature(self, train_landmark_dir, val_seen_landmark_dir, val_unseen_landmark_dir, aug_landmark_dir, test_landmark_dir=None):
        landmark_dict = {}
        landmark_dict1 = np.load(train_landmark_dir, allow_pickle=True).item()
        landmark_dict2 = np.load(val_seen_landmark_dir, allow_pickle=True).item()
        landmark_dict3 = np.load(val_unseen_landmark_dir, allow_pickle=True).item()
        #landmark_dict4 = np.load(test_landmark_dir, allow_pickle=True).item()
        #landmark_dict5 = np.load(aug_landmark_dir, allow_pickle=True).item()

        landmark_dict.update(landmark_dict1)
        landmark_dict.update(landmark_dict2)
        landmark_dict.update(landmark_dict3)
        #landmark_dict.update(landmark_dict4)
        #landmark_dict.update(landmark_dict5)

        return landmark_dict

    def get_landmark_triplets(self, train_triplet_dir, val_seen_triplet_dir, val_unseen_triplet_dir):
        landmark_triplet_vector = {}
        landmark_triplet1 = np.load(train_triplet_dir, allow_pickle=True).item()
        landmark_triplet2 = np.load(val_seen_triplet_dir, allow_pickle=True).item()
        landmark_triplet3 = np.load(val_unseen_triplet_dir, allow_pickle=True).item()
        landmark_triplet_vector.update(landmark_triplet1)
        landmark_triplet_vector.update(landmark_triplet2)
        landmark_triplet_vector.update(landmark_triplet3)
        return landmark_triplet_vector

    def cartesian_product(self,x,y):
        xs = x.size()
        ys = y.size()
        assert xs[0] == ys[0]
        # torch cat is not broadcasting, do repeat manually
        xx = x.view(xs[0], xs[1], 1, xs[2]).repeat(1, 1, ys[1], 1)
        yy = y.view(ys[0], 1, ys[1], ys[2]).repeat(1, xs[1], 1, 1)
        return torch.cat([xx, yy], dim=-1)

    def _candidate_variable1(self, obs):
        candidate_leng = [len(ob['candidate']) + 1 for ob in obs]       # +1 is for the end

        candidate_feat = np.zeros((len(obs), max(candidate_leng), self.feature_size + args.angle_feat_size), dtype=np.float32)
        # Note: The candidate_feat at len(ob['candidate']) is the feature for the END
        # which is zero in my implementation
        for i, ob in enumerate(obs):
            for j, c in enumerate(ob['candidate']):
                candidate_feat[i, j, :] = c['feature']                         # Image feat
        return torch.from_numpy(candidate_feat).cuda(), candidate_leng

    def _candidate_variable2(self, obs):
        object_num = 36
        feature_size1 = 300
        feature_size2 = 152
        view_size = 4
        candidate_index = []
        batch_size = len(obs)

        #candidate_leng = args.candidate_length + 1 # +1 is for the end
        candidate_len_list = [len(ob['candidate']) + 1 for ob in obs] #maybe change later
        candidate_leng = max(candidate_len_list)

        navigable_obj_text_feat = torch.zeros(batch_size, candidate_leng, object_num, feature_size1)
        object_mask = torch.zeros(batch_size, candidate_leng, object_num)
        navigable_obj_img_feat = torch.zeros(batch_size, candidate_leng, object_num, feature_size2)
        navigable_obj_rel_feat = torch.zeros(batch_size, candidate_leng, object_num, object_num, 12)
        view_feat = torch.zeros(batch_size, candidate_leng, view_size)

        # Note: The candidate_feat at len(ob['candidate']) is the feature for the END
        # which is zero in my implementation
        for i, ob in enumerate(obs):
            tmp_candidate_index = []
            bottom_index_list = []
            middle_index_list = []
            top_index_list = []
            heading_list = []
            view_list = []

            for j, c in enumerate(ob['candidate']):
                tmp_candidate_index.append(c['pointId']) # did not include itself
                bottom_index, middle_index, top_index = self.elevation_index(c['pointId'])
                bottom_index_list.append(bottom_index)
                middle_index_list.append(middle_index)
                top_index_list.append(top_index)
                heading_list.append(c['normalized_heading'])
                view_list.append(c['relative_position'])
            
            interval_len = len(bottom_index_list)
            candidate_index.append(tmp_candidate_index)
            tmp_obj_text_feat, tmp_mask, tmp_obj_img_feat, tmp_obj_relation = self._faster_rcnn_feature(ob, heading_list)
            navigable_obj_text_feat[i,:interval_len,:,:] = tmp_obj_text_feat
            object_mask[i,:interval_len,:] = tmp_mask
            #navigable_obj_img_feat[i,:interval_len,:,:] = tmp_obj_img_feat
            navigable_obj_rel_feat[i,:interval_len,:,:,:] = tmp_obj_relation
            view_feat[i,:len(view_list)] = torch.tensor(view_list)
            
            #image feature
            # candidate_img_feat[i, 0:interval_len,:] = pano_img_feat[i,bottom_index_list]
            # candidate_img_feat[i, candidate_leng:candidate_leng+interval_len,:] = pano_img_feat[i,middle_index_list]    
            # candidate_img_feat[i, 2*candidate_leng:2*candidate_leng+interval_len,:] = pano_img_feat[i,top_index_list]
            #object feature
            # self.distribute_feature(interval_len, navigable_obj_text_feat[i], tmp_obj_text_feat, candidate_leng)
            # self.distribute_feature(interval_len, object_mask[i], tmp_mask, candidate_leng)
            # self.distribute_feature(interval_len, navigable_obj_img_feat[i], tmp_obj_img_feat, candidate_leng)
        # candidate_leng condtain itself, but candidate_index does not contain itself    
        return navigable_obj_text_feat, object_mask, navigable_obj_img_feat, candidate_index, navigable_obj_rel_feat, view_feat

    def distribute_feature(self, interval_len, pre_feature, feature, candidate_leng):
        pre_feature[0: interval_len] =  feature[0*interval_len:1*interval_len]
        pre_feature[candidate_leng:candidate_leng+interval_len] =  feature[1*interval_len:2*interval_len]
        pre_feature[2*candidate_leng:2*candidate_leng+interval_len] =  feature[2*interval_len:3*interval_len]
    
    def elevation_index(self, index):
        elevation_level = index % 12
        bottom_index = elevation_level
        middle_index = elevation_level + 12
        top_index = elevation_level + 12 + 12
        return bottom_index, middle_index, top_index
    
    def _faster_rcnn_feature(self, ob, heading_list):
        obj_img_feature = []
        obj_text_feature = []
        obj_mask = []
        object_text = []
        obj_relation = []
        #elevation_list = [30*math.pi/180, 0*math.pi/180, -30*math.pi/180]
        elevation_list = [0*math.pi/180]

        for elevation in elevation_list:
            for heading in heading_list:
                temp = int(round((heading*180/math.pi)/30) * 30)
                if temp >=  360:
                    temp = temp - 360      
                elif temp < 0 :
                    temp = temp + 360
                obj_text_feature.append(self.obj_feat[ob['scan']][ob['viewpoint']][str(temp*math.pi/180)+'_'+ str(elevation)]['text_feature'])
                obj_mask.append(self.obj_feat[ob['scan']][ob['viewpoint']][str(temp*math.pi/180)+'_'+ str(elevation)]['text_mask'])
                #obj_img_feature.append(self.obj_feat[ob['scan']][ob['viewpoint']][str(temp*math.pi/180)+'_'+ str(elevation)]['features'])
                obj_relation.append(self.obj_feat[ob['scan']][ob['viewpoint']][str(temp*math.pi/180)+'_'+ str(elevation)]['relation'])

        obj_text_feature = torch.stack(obj_text_feature, dim=0)
        obj_mask = torch.stack(obj_mask, dim=0)
        obj_img_feature = None
        #obj_img_feature = torch.stack(obj_img_feature, dim=0)
        obj_relation = torch.stack(obj_relation, dim=0)
        return obj_text_feature, obj_mask, obj_img_feature, obj_relation

    def get_input_feat(self, obs):
        input_a_t = np.zeros((len(obs), args.angle_feat_size), np.float32)
        for i, ob in enumerate(obs):
            input_a_t[i] = utils.angle_feature(ob['heading'], ob['elevation'])
        input_a_t = torch.from_numpy(input_a_t).cuda()

        f_t = self._feature_variable(obs)      # Image features from obs
        candidate_feat, candidate_leng = self._candidate_variable1(obs)
        candidate_obj_text_feat, object_mask, candidate_obj_img_feat, candidate_index, candidate_relation, view_feat = self._candidate_variable2(obs)
        return input_a_t, f_t, candidate_feat, candidate_obj_text_feat, object_mask, candidate_obj_img_feat, candidate_leng, candidate_index, candidate_relation, view_feat
        #return input_a_t, f_t,candidate_feat,candidate_leng
    
    def get_cand_object_feat(self, obs):
        top_N_obj = 36
        candidate_leng = [len(ob['candidate']) + 1 for ob in obs]

        batch_size = len(obs); max_cand = max(candidate_leng)
        temp_obj_label = np.zeros((batch_size, max_cand, top_N_obj), np.float32)
        for i, ob in enumerate(obs):
            for j, c in enumerate(ob['cand_objects']):
                temp_obj_label[i, j, :] = c

        temp_obj_label = temp_obj_label.reshape(batch_size, max_cand*top_N_obj)
        cand_obj_label_enc = self.objencoder(torch.from_numpy(temp_obj_label).cuda().long())

        cand_obj_label = cand_obj_label_enc.reshape(batch_size, max_cand, top_N_obj, self.glove_dim)
        cand_object_feat = Variable(cand_obj_label, requires_grad=False).cuda()

        return cand_object_feat

    def _teacher_action(self, obs, ended):
        """
        Extract teacher actions into variable.
        :param obs: The observation.
        :param ended: Whether the action seq is ended
        :return:
        """
        a = np.zeros(len(obs), dtype=np.int64)
        for i, ob in enumerate(obs):
            if ended[i]:                                            # Just ignore this index
                a[i] = args.ignoreid
            else:
                for k, candidate in enumerate(ob['candidate']):
                    if candidate['viewpointId'] == ob['teacher']:   # Next view point
                        a[i] = k
                        break
                else:   # Stop here
                    assert ob['teacher'] == ob['viewpoint']         # The teacher action should be "STAY HERE"
                    a[i] = len(ob['candidate'])
        return torch.from_numpy(a).cuda()

    def make_equiv_action(self, a_t, perm_obs, perm_idx=None, traj=None):
        """
        Interface between Panoramic view and Egocentric view 
        It will convert the action panoramic view action a_t to equivalent egocentric view actions for the simulator
        """
        def take_action(i, idx, name):
            if type(name) is int:       # Go to the next view
                self.env.env.sims[idx].makeAction(name, 0, 0)
            else:                       # Adjust
                self.env.env.sims[idx].makeAction(*self.env_actions[name])
            state = self.env.env.sims[idx].getState()
            if traj is not None:
                traj[i]['path'].append((state.location.viewpointId, state.heading, state.elevation))
        if perm_idx is None:
            perm_idx = range(len(perm_obs))
        for i, idx in enumerate(perm_idx):
            action = a_t[i]
            if action != -1:            # -1 is the <stop> action
                select_candidate = perm_obs[i]['candidate'][action]
                src_point = perm_obs[i]['viewIndex']
                trg_point = select_candidate['pointId']
                src_level = (src_point ) // 12   # The point idx started from 0
                trg_level = (trg_point ) // 12
                while src_level < trg_level:    # Tune up
                    take_action(i, idx, 'up')
                    src_level += 1
                while src_level > trg_level:    # Tune down
                    take_action(i, idx, 'down')
                    src_level -= 1
                while self.env.env.sims[idx].getState().viewIndex != trg_point:    # Turn right until the target
                    take_action(i, idx, 'right')
                assert select_candidate['viewpointId'] == \
                       self.env.env.sims[idx].getState().navigableLocations[select_candidate['idx']].viewpointId
                take_action(i, idx, select_candidate['idx'])

    def rollout(self, train_ml=None, train_rl=True, reset=True, speaker=None):
        """
        :param train_ml:    The weight to train with maximum likelihood
        :param train_rl:    whether use RL in training
        :param reset:       Reset the environment
        :param speaker:     Speaker used in back translation.
                            If the speaker is not None, use back translation.
                            O.w., normal training
        :return:
        """
        
        if self.feedback == 'teacher' or self.feedback == 'argmax':
            train_rl = False

        if reset:
            # Reset env
            obs = np.array(self.env.reset())
        else:
            obs = np.array(self.env._get_obs())

        batch_size = len(obs)

        if speaker is not None:         # Trigger the self_train mode!
            noise = self.decoder.drop_env(torch.ones(self.feature_size).cuda())
            batch = self.env.batch.copy()
            speaker.env = self.env
            insts = speaker.infer_batch(featdropmask=noise)     # Use the same drop mask in speaker

            # Create fake environments with the generated instruction
            boss = np.ones((batch_size, 1), np.int64) * self.tok.word_to_index['<BOS>']  # First word is <BOS>
            insts = np.concatenate((boss, insts), 1)
            for i, (datum, inst) in enumerate(zip(batch, insts)):
                if inst[-1] != self.tok.word_to_index['<PAD>']: # The inst is not ended!
                    inst[-1] = self.tok.word_to_index['<EOS>']
                datum.pop('instructions')
                datum.pop('instr_encoding')
                datum['instructions'] = self.tok.decode_sentence(inst)
                datum['instr_encoding'] = inst
            obs = np.array(self.env.reset(batch))

        # Reorder the language input for the encoder (do not ruin the original code)
        seq, seq_mask, seq_lengths, perm_idx = self._sort_batch(obs)
        perm_obs = obs[perm_idx]
        sentence = []
        config_num_list = []

        landmark_obj_feat_list = [] 
        motion_feat_list = []
        landmark_num_list = []
        triplet_num_list = []
        view_num_list = []
        instr_id_list = []

        for ob_id, each_ob in enumerate(perm_obs):
            instr_id_list.append(each_ob['instr_id']) 
            config_num_list.append(len(each_ob['configurations']))
            sentence.append(each_ob['instructions'])
            tmp_landmark_num_list = []
            tmp_triplet_num_list = []
            tmp_view_num_list = []
            for config_id, each_config in enumerate(each_ob['configurations']):
                tmp_landmark_num_list.append(len(self.landmark_feature[each_ob['instr_id']+"_"+str(config_id)][0]))
                tmp_triplet_num_list.append(len(self.landmark_triplet_vector[each_ob['instr_id']+"_"+str(config_id)]['triplet']))
                tmp_view_num_list.append(len(self.landmark_triplet_vector[each_ob['instr_id']+"_"+str(config_id)]['view']))
            landmark_num_list.append(max(tmp_landmark_num_list))
            triplet_num_list.append(max(tmp_triplet_num_list))
            view_num_list.append(max(tmp_view_num_list))
        
        max_config_num = max(config_num_list)
        max_landmark_num = max(landmark_num_list)
        max_triplet_num = max(triplet_num_list) if max(triplet_num_list) != 0 else 1
        max_view_num = max(view_num_list) if max(view_num_list) != 0 else 1
        
        landmark_object_feature = torch.zeros(batch_size, max_config_num, max_landmark_num, args.text_dimension).cuda()
        landmark_mask = torch.zeros(batch_size).cuda()
        motion_indicator_tensor = torch.zeros(batch_size, max_config_num, args.text_dimension).cuda()
        landmark_triplet_feature = torch.zeros(batch_size, max_config_num, max_triplet_num, 3, args.text_dimension).cuda()
        view_feature = torch.zeros(batch_size, max_config_num, max_view_num, 3, args.text_dimension).cuda()
        view_mask = torch.zeros(batch_size).cuda()


        for ob_id, each_ob in enumerate(perm_obs):
            tmp_landmark_mask = []
            tmp_view_mask = []
            for config_id, each_config in enumerate(each_ob['configurations']):
                tmp_landmark_tuple = self.landmark_feature[each_ob['instr_id']+"_"+str(config_id)][0]
                tmp_landmark_mask.append(self.landmark_feature[each_ob['instr_id']+"_"+str(config_id)][1])

                tmp_motion_indi_feat = torch.from_numpy(self.motion_indicator_feature[each_ob['instr_id']+"_"+str(config_id)][1])
                
                tmp_triplet_array = self.landmark_triplet_vector[each_ob["instr_id"]+"_"+str(config_id)]['triplet_vector']

                tmp_view_array = self.landmark_triplet_vector[each_ob["instr_id"]+"_"+str(config_id)]['view_vector']
                tmp_view_mask.append(self.landmark_triplet_vector[each_ob["instr_id"]+"_"+str(config_id)]['view_mask'])

                tmp_landmark_tensor = torch.from_numpy(np.stack(list(zip(*tmp_landmark_tuple))[1], axis=0))
                if not tmp_triplet_array:
                    tmp_triplet_array = np.zeros((1,3,args.text_dimension))
                if not tmp_view_array:
                    tmp_view_array = np.zeros((1,3,args.text_dimension))
                tmp_triplet_tensor = torch.from_numpy(np.stack(tmp_triplet_array,axis=0))
                tmp_view_tensor = torch.from_numpy(np.stack(tmp_view_array, axis=0))
                landmark_object_feature[ob_id, config_id, :len(tmp_landmark_tuple),:] = tmp_landmark_tensor
                motion_indicator_tensor[ob_id, config_id,:] = tmp_motion_indi_feat
                landmark_triplet_feature[ob_id, config_id, :len(tmp_triplet_array),:,:] = tmp_triplet_tensor
                view_feature[ob_id, config_id, :len(tmp_view_array),:,:] = tmp_view_tensor 
            if sum(tmp_landmark_mask) == 0:
                landmark_mask[ob_id] = 0
            else:
                landmark_mask[ob_id] = 1
            
            if sum(tmp_view_mask) == 0:
                view_mask[ob_id] = 0
            else:
                view_mask[ob_id] = 1
                     
       # ctx, h_t, c_t = self.encoder(sentence_list, seq_lengths)
        tmp_ctx, h_t, c_t, tmp_ctx_mask, split_index = self.encoder(sentence, seq_len=seq_lengths)

        token_num = max([each_split[-1] for each_split in split_index])
        #assert max_config_num == max([len(each_split) for each_split in split_index]) 
        # There are cases that are statisfied with this assertion statement when the length of the sentence larger than 80. So here we just heuritically set all equals max_config_num
        max_config_num = max(config_num_list)
        bert_ctx = torch.zeros(batch_size, max_config_num, token_num, 512, device = tmp_ctx.device)
        bert_ctx_mask = torch.zeros(batch_size, max_config_num, token_num, device = tmp_ctx.device)
        bert_cls = torch.zeros(batch_size, max_config_num, 512, device = tmp_ctx.device)
        bert_cls_mask = torch.zeros(batch_size, max_config_num, device = tmp_ctx.device)

        for ob_id, each_index_list in enumerate(split_index):
            start = 0
            for list_id, each_index in enumerate(each_index_list):
                end = each_index
                bert_ctx[ob_id,list_id,0:end-start,:] = tmp_ctx[ob_id,start:end,:]
                bert_ctx_mask[ob_id, list_id,0:end-start] = tmp_ctx_mask[ob_id,start:end]
                bert_cls[ob_id, list_id, :] = tmp_ctx[ob_id, each_index,:]
                bert_cls_mask[ob_id, list_id] = 1
                start = end + 1
        
        atten_ctx1, attn = self.encoder.sf(bert_cls, bert_cls_mask, bert_ctx, bert_ctx_mask)
        #ctx = bert_ctx
        atten_ctx2, attn = self.encoder.sf(motion_indicator_tensor, bert_cls_mask, bert_ctx, bert_ctx_mask)
        atten_ctx3, attn = self.encoder.sf(torch.mean(landmark_object_feature, dim=2), bert_cls_mask, bert_ctx, bert_ctx_mask)
        #ctx = torch.cat([attend_ctx, torch.mean(landmark_object_feature, dim=2), motion_indicator_tensor], dim=2)   
        ctx = torch.mean(torch.stack([atten_ctx1, atten_ctx2, atten_ctx3],dim=2), dim=2)
        ctx_mask = (bert_cls_mask==0).byte()

        # Init the reward shaping
        last_dist = np.zeros(batch_size, np.float32)
        for i, ob in enumerate(perm_obs):   # The init distance from the view point to the target
            last_dist[i] = ob['distance']

        # Record starting point
        traj = [{
            'instr_id': ob['instr_id'],
            'path': [(ob['viewpoint'], ob['heading'], ob['elevation'])]
        } for ob in perm_obs]

        # For test result submission
        visited = [set() for _ in perm_obs]

        # Initialization the tracking state
        ended = np.array([False] * batch_size)  # Indices match permuation of the model, not env

        # Init the logs
        rewards = []
        hidden_states = []
        policy_log_probs = []
        masks = []
        entropys = []
        ml_loss = 0.

        h1 = h_t

        #state attention
        s0 = torch.zeros(batch_size, max_config_num, requires_grad=False).cuda()
        r0 = torch.zeros(batch_size,2, requires_grad=False).cuda()
        s0[:,0] = 1
        r0[:,0] = 1
        ctx_attn = s0

        for t in range(self.episode_len):
            #input_a_t, f_t, candidate_feat, candidate_leng = self.get_input_feat(perm_obs)
            #candidate_obj_text_feat = self.get_cand_object_feat(perm_obs)

            input_a_t, f_t, candidate_feat, candidate_obj_text_feat, object_mask, candidate_obj_img_feat, candidate_leng, candidate_index, candidat_relation, view_img_feat = self.get_input_feat(perm_obs)
            candidate_obj_text_feat = candidate_obj_text_feat.cuda()
            # candidat_relation = candidat_relation.cuda()
            view_img_feat = view_img_feat.cuda()
            #candidat_relation = candidat_relation.cuda()
            # object_mask = object_mask.cuda()
            # candidate_obj_img_feat = candidate_obj_img_feat.cuda()
            
            if speaker is not None:       # Apply the env drop mask to the feat
                candidate_feat[..., :-args.angle_feat_size] *= noise
                f_t[..., :-args.angle_feat_size] *= noise

            r_t = r0 if t==0 else None

            #landamrk similarity
           
            batch_size, image_num, object_num, obj_feat_dim = candidate_obj_text_feat.shape
            landmark_object_feature = landmark_object_feature.view(batch_size, max_config_num*max_landmark_num, 300) #batch * 180 * 300
            landmark_similarity = torch.matmul(candidate_obj_text_feat, torch.transpose(landmark_object_feature.unsqueeze(1),3,2))# (4*48*36*300) * (4*1*300*180）-> 4 * 48 * 36*180
            landmark_similarity = landmark_similarity.view(batch_size, image_num, object_num, max_config_num, max_landmark_num) # 4 * 48 * 36 * 15 * 12
            landmark_similarity = torch.max(landmark_similarity, dim=-1)[0]
            #view_object_similarity = torch.matmul(candidate_obj_text_feat,torch.transpose(view_feature[:,:,:,0].view(batch_size, -1,300).unsqueeze(1),3,2)).view(batch_size, image_num, object_num, max_config_num, -1)
            #obj_rel_arg_feat = self.cartesian_product(candidate_obj_text_feat.view(-1,object_num,obj_feat_dim), candidate_obj_text_feat.view(-1,object_num,obj_feat_dim)).view(batch_size, image_num, object_num, object_num, obj_feat_dim*2)   
           
            view_object_similarity = None
            obj_rel_arg_feat = None

            h_t, c_t, logit, h1, ctx_attn = self.decoder(input_a_t, f_t, candidate_feat,
                                               h_t, h1, c_t,
                                               ctx, t, ctx_attn, r_t, ctx_mask, landmark_similarity = landmark_similarity, landmark_mask=landmark_mask,
                                               landmark_triplet_feature=landmark_triplet_feature, obj_rel_arg_feat=obj_rel_arg_feat, candidat_relation=candidat_relation, 
                                               view_img_feat=view_img_feat, view_feature=view_feature, view_object_similarity=view_object_similarity, view_mask=view_mask,
                                               already_dropfeat=(speaker is not None))

            hidden_states.append(h_t)

            # Mask outputs where agent can't move forward
            # Here the logit is [b, max_candidate]
            
            candidate_mask = utils.length2mask(candidate_leng)
            if args.submit:     # Avoding cyclic path
                for ob_id, ob in enumerate(perm_obs):
                    visited[ob_id].add(ob['viewpoint'])
                    for c_id, c in enumerate(ob['candidate']):
                        if c['viewpointId'] in visited[ob_id]:
                            candidate_mask[ob_id][c_id] = 1
            logit.masked_fill_(candidate_mask, -float('inf'))

            # Supervised training
            target = self._teacher_action(perm_obs, ended)
            ml_loss += self.criterion(logit, target)

            # Determine next model inputs
            if self.feedback == 'teacher':
                a_t = target                # teacher forcing
            elif self.feedback == 'argmax': 
                _, a_t = logit.max(1)        # student forcing - argmax
                a_t = a_t.detach()
                log_probs = F.log_softmax(logit, 1)                              # Calculate the log_prob here
                policy_log_probs.append(log_probs.gather(1, a_t.unsqueeze(1)))   # Gather the log_prob for each batch
            elif self.feedback == 'sample':
                probs = F.softmax(logit, 1)    # sampling an action from model
                c = torch.distributions.Categorical(probs)
                self.logs['entropy'].append(c.entropy().sum().item())      # For log
                entropys.append(c.entropy())                                # For optimization
                a_t = c.sample().detach()
                policy_log_probs.append(c.log_prob(a_t))
            else:
                print(self.feedback)
                sys.exit('Invalid feedback option')

            # Prepare environment action
            # NOTE: Env action is in the perm_obs space
            cpu_a_t = a_t.cpu().numpy()
            for i, next_id in enumerate(cpu_a_t):
                if next_id == (candidate_leng[i]-1) or next_id == args.ignoreid or ended[i]:    # The last action is <end>
                    cpu_a_t[i] = -1             # Change the <end> and ignore action to -1

            # Make action and get the new state
            self.make_equiv_action(cpu_a_t, perm_obs, perm_idx, traj)
            obs = np.array(self.env._get_obs())
            perm_obs = obs[perm_idx]                    # Perm the obs for the resu

            # Calculate the mask and reward
            dist = np.zeros(batch_size, np.float32)
            reward = np.zeros(batch_size, np.float32)
            mask = np.ones(batch_size, np.float32)
            for i, ob in enumerate(perm_obs):
                dist[i] = ob['distance']
                if ended[i]:            # If the action is already finished BEFORE THIS ACTION.
                    reward[i] = 0.
                    mask[i] = 0.
                else:       # Calculate the reward
                    action_idx = cpu_a_t[i]
                    if action_idx == -1:        # If the action now is end
                        if dist[i] < 3:         # Correct
                            reward[i] = 2.
                        else:                   # Incorrect
                            reward[i] = -2.
                    else:                       # The action is not end
                        reward[i] = - (dist[i] - last_dist[i])      # Change of distance
                        if reward[i] > 0:                           # Quantification
                            reward[i] = 1
                        elif reward[i] < 0:
                            reward[i] = -1
                        else:
                            raise NameError("The action doesn't change the move")
            rewards.append(reward)
            masks.append(mask)
            last_dist[:] = dist

            # Update the finished actions
            # -1 means ended or ignored (already ended)
            ended[:] = np.logical_or(ended, (cpu_a_t == -1))

            # Early exit if all ended
            if ended.all(): 
                break

        if train_rl:
            # Last action in A2C
            #
            # input_a_t, f_t, candidate_feat, candidate_leng = self.get_input_feat(perm_obs)
            
            input_a_t, f_t, candidate_feat, candidate_obj_text_feat, object_mask, candidate_obj_img_feat, candidate_leng, candidate_index, candidat_relation, view_img_feat = self.get_input_feat(perm_obs)
            candidate_obj_text_feat = candidate_obj_text_feat.cuda()
            # candidat_relation = candidat_relation.cuda()
            view_img_feat = view_img_feat.cuda()
            #candidat_relation = candidat_relation.cuda()
            # object_mask = object_mask.cuda()
            # candidate_obj_img_feat = candidate_obj_img_feat.cuda()
            
            if speaker is not None:       # Apply the env drop mask to the feat
                candidate_feat[..., :-args.angle_feat_size] *= noise
                f_t[..., :-args.angle_feat_size] *= noise

            r_t = r0 if t==0 else None

            #landamrk similarity
           
            batch_size, image_num, object_num, obj_feat_dim = candidate_obj_text_feat.shape
            landmark_object_feature = landmark_object_feature.view(batch_size, max_config_num*max_landmark_num, 300) #batch * 180 * 300
            landmark_similarity = torch.matmul(candidate_obj_text_feat, torch.transpose(landmark_object_feature.unsqueeze(1),3,2))# (4*48*36*300) * (4*1*300*180）-> 4 * 48 * 36*180
            landmark_similarity = landmark_similarity.view(batch_size, image_num, object_num, max_config_num, max_landmark_num) # 4 * 48 * 36 * 15 * 12
            landmark_similarity = torch.max(landmark_similarity, dim=-1)[0]
            #view_object_similarity = torch.matmul(candidate_obj_text_feat,torch.transpose(view_feature[:,:,:,0].view(batch_size, -1,300).unsqueeze(1),3,2)).view(batch_size, image_num, object_num, max_config_num, -1)
            #obj_rel_arg_feat = self.cartesian_product(candidate_obj_text_feat.view(-1,object_num,obj_feat_dim), candidate_obj_text_feat.view(-1,object_num,obj_feat_dim)).view(batch_size, image_num, object_num, object_num, obj_feat_dim*2)   
          
            view_object_similarity = None
            obj_rel_arg_feat = None

            last_h_, _, _, _,_ = self.decoder(input_a_t, f_t, candidate_feat,
                                               h_t, h1, c_t,
                                               ctx, t, ctx_attn, r_t, ctx_mask, landmark_similarity = landmark_similarity, landmark_mask=landmark_mask,
                                               landmark_triplet_feature=landmark_triplet_feature, obj_rel_arg_feat=obj_rel_arg_feat, candidat_relation=candidat_relation, 
                                               view_img_feat=view_img_feat, view_feature=view_feature, view_object_similarity=view_object_similarity, view_mask=view_mask,
                                               already_dropfeat=(speaker is not None))
            rl_loss = 0.

            # NOW, A2C!!!
            # Calculate the final discounted reward
            last_value__ = self.critic(last_h_).detach()    # The value esti of the last state, remove the grad for safety
            discount_reward = np.zeros(batch_size, np.float32)  # The inital reward is zero
            for i in range(batch_size):
                if not ended[i]:        # If the action is not ended, use the value function as the last reward
                    discount_reward[i] = last_value__[i]

            length = len(rewards)
            total = 0
            for t in range(length-1, -1, -1):
                discount_reward = discount_reward * args.gamma + rewards[t]   # If it ended, the reward will be 0
                mask_ = Variable(torch.from_numpy(masks[t]), requires_grad=False).cuda()
                clip_reward = discount_reward.copy()
                r_ = Variable(torch.from_numpy(clip_reward), requires_grad=False).cuda()
                v_ = self.critic(hidden_states[t])
                a_ = (r_ - v_).detach()

                # r_: The higher, the better. -ln(p(action)) * (discount_reward - value)
                rl_loss += (-policy_log_probs[t] * a_ * mask_).sum()
                rl_loss += (((r_ - v_) ** 2) * mask_).sum() * 0.5     # 1/2 L2 loss
                if self.feedback == 'sample':
                    rl_loss += (- 0.01 * entropys[t] * mask_).sum()
                self.logs['critic_loss'].append((((r_ - v_) ** 2) * mask_).sum().item())

                total = total + np.sum(masks[t])
            self.logs['total'].append(total)

            # Normalize the loss function
            if args.normalize_loss == 'total':
                rl_loss /= total
            elif args.normalize_loss == 'batch':
                rl_loss /= batch_size
            else:
                assert args.normalize_loss == 'none'

            self.loss += rl_loss

        if train_ml is not None:
            self.loss += ml_loss * train_ml / batch_size

        if type(self.loss) is int:  # For safety, it will be activated if no losses are added
            self.losses.append(0.)
        else:
            self.losses.append(self.loss.item() / self.episode_len)    # This argument is useless.

        return traj

    def _dijkstra(self):
        """
        The dijkstra algorithm.
        Was called beam search to be consistent with existing work.
        But it actually finds the Exact K paths with smallest listener log_prob.
        :return:
        [{
            "scan": XXX
            "instr_id":XXX,
            'instr_encoding": XXX
            'dijk_path': [v1, v2, ..., vn]      (The path used for find all the candidates)
            "paths": {
                    "trajectory": [viewpoint_id1, viewpoint_id2, ..., ],
                    "action": [act_1, act_2, ..., ],
                    "listener_scores": [log_prob_act1, log_prob_act2, ..., ],
                    "visual_feature": [(f1_step1, f2_step2, ...), (f1_step2, f2_step2, ...)
            }
        }]
        """
        def make_state_id(viewpoint, action):     # Make state id
            return "%s_%s" % (viewpoint, str(action))
        def decompose_state_id(state_id):     # Make state id
            viewpoint, action = state_id.split("_")
            action = int(action)
            return viewpoint, action

        # Get first obs
        obs = self.env._get_obs()

        # Prepare the state id
        batch_size = len(obs)
        results = [{"scan": ob['scan'],
                    "instr_id": ob['instr_id'],
                    "instr_encoding": ob["instr_encoding"],
                    "dijk_path": [ob['viewpoint']],
                    "paths": []} for ob in obs]

        # Encoder
        seq, seq_mask, seq_lengths, perm_idx = self._sort_batch(obs)
        recover_idx = np.zeros_like(perm_idx)
        for i, idx in enumerate(perm_idx):
            recover_idx[idx] = i
        ctx, h_t, c_t = self.encoder(seq, seq_lengths)
        ctx, h_t, c_t, ctx_mask = ctx[recover_idx], h_t[recover_idx], c_t[recover_idx], seq_mask[recover_idx]    # Recover the original order

        # Dijk Graph States:
        id2state = [
            {make_state_id(ob['viewpoint'], -95):
                 {"next_viewpoint": ob['viewpoint'],
                  "running_state": (h_t[i], h_t[i], c_t[i]),
                  "location": (ob['viewpoint'], ob['heading'], ob['elevation']),
                  "feature": None,
                  "from_state_id": None,
                  "score": 0,
                  "scores": [],
                  "actions": [],
                  }
             }
            for i, ob in enumerate(obs)
        ]    # -95 is the start point
        visited = [set() for _ in range(batch_size)]
        finished = [set() for _ in range(batch_size)]
        graphs = [utils.FloydGraph() for _ in range(batch_size)]        # For the navigation path
        ended = np.array([False] * batch_size)

        # Dijk Algorithm
        for _ in range(300):
            # Get the state with smallest score for each batch
            # If the batch is not ended, find the smallest item.
            # Else use a random item from the dict  (It always exists)
            smallest_idXstate = [
                max(((state_id, state) for state_id, state in id2state[i].items() if state_id not in visited[i]),
                    key=lambda item: item[1]['score'])
                if not ended[i]
                else
                next(iter(id2state[i].items()))
                for i in range(batch_size)
            ]

            # Set the visited and the end seqs
            for i, (state_id, state) in enumerate(smallest_idXstate):
                assert (ended[i]) or (state_id not in visited[i])
                if not ended[i]:
                    viewpoint, action = decompose_state_id(state_id)
                    visited[i].add(state_id)
                    if action == -1:
                        finished[i].add(state_id)
                        if len(finished[i]) >= args.candidates:     # Get enough candidates
                            ended[i] = True

            # Gather the running state in the batch
            h_ts, h1s, c_ts = zip(*(idXstate[1]['running_state'] for idXstate in smallest_idXstate))
            h_t, h1, c_t = torch.stack(h_ts), torch.stack(h1s), torch.stack(c_ts)

            # Recover the env and gather the feature
            for i, (state_id, state) in enumerate(smallest_idXstate):
                next_viewpoint = state['next_viewpoint']
                scan = results[i]['scan']
                from_viewpoint, heading, elevation = state['location']
                self.env.env.sims[i].newEpisode(scan, next_viewpoint, heading, elevation) # Heading, elevation is not used in panoramic
            obs = self.env._get_obs()

            # Update the floyd graph
            # Only used to shorten the navigation length
            # Will not effect the result
            for i, ob in enumerate(obs):
                viewpoint = ob['viewpoint']
                if not graphs[i].visited(viewpoint):    # Update the Graph
                    for c in ob['candidate']:
                        next_viewpoint = c['viewpointId']
                        dis = self.env.distances[ob['scan']][viewpoint][next_viewpoint]
                        graphs[i].add_edge(viewpoint, next_viewpoint, dis)
                    graphs[i].update(viewpoint)
                results[i]['dijk_path'].extend(graphs[i].path(results[i]['dijk_path'][-1], viewpoint))

            input_a_t, f_t, candidate_feat, candidate_leng = self.get_input_feat(obs)

            # Run one decoding step
            h_t, c_t, alpha, logit, h1 = self.decoder(input_a_t, f_t, candidate_feat,
                                                      h_t, h1, c_t,
                                                      ctx, ctx_mask,
                                                      False)

            # Update the dijk graph's states with the newly visited viewpoint
            candidate_mask = utils.length2mask(candidate_leng)
            logit.masked_fill_(candidate_mask, -float('inf'))
            log_probs = F.log_softmax(logit, 1)                              # Calculate the log_prob here
            _, max_act = log_probs.max(1)

            for i, ob in enumerate(obs):
                current_viewpoint = ob['viewpoint']
                candidate = ob['candidate']
                current_state_id, current_state = smallest_idXstate[i]
                old_viewpoint, from_action = decompose_state_id(current_state_id)
                assert ob['viewpoint'] == current_state['next_viewpoint']
                if from_action == -1 or ended[i]:       # If the action is <end> or the batch is ended, skip it
                    continue
                for j in range(len(ob['candidate']) + 1):               # +1 to include the <end> action
                    # score + log_prob[action]
                    modified_log_prob = log_probs[i][j].detach().cpu().item() 
                    new_score = current_state['score'] + modified_log_prob
                    if j < len(candidate):                        # A normal action
                        next_id = make_state_id(current_viewpoint, j)
                        next_viewpoint = candidate[j]['viewpointId']
                        trg_point = candidate[j]['pointId']
                        heading = (trg_point % 12) * math.pi / 6
                        elevation = (trg_point // 12 - 1) * math.pi / 6
                        location = (next_viewpoint, heading, elevation)
                    else:                                                 # The end action
                        next_id = make_state_id(current_viewpoint, -1)    # action is -1
                        next_viewpoint = current_viewpoint                # next viewpoint is still here
                        location = (current_viewpoint, ob['heading'], ob['elevation'])

                    if next_id not in id2state[i] or new_score > id2state[i][next_id]['score']:
                        id2state[i][next_id] = {
                            "next_viewpoint": next_viewpoint,
                            "location": location,
                            "running_state": (h_t[i], h1[i], c_t[i]),
                            "from_state_id": current_state_id,
                            "feature": (f_t[i].detach().cpu(), candidate_feat[i][j].detach().cpu()),
                            "score": new_score,
                            "scores": current_state['scores'] + [modified_log_prob],
                            "actions": current_state['actions'] + [len(candidate)+1],
                        }

            # The active state is zero after the updating, then setting the ended to True
            for i in range(batch_size):
                if len(visited[i]) == len(id2state[i]):     # It's the last active state
                    ended[i] = True

            # End?
            if ended.all():
                break

        # Move back to the start point
        for i in range(batch_size):
            results[i]['dijk_path'].extend(graphs[i].path(results[i]['dijk_path'][-1], results[i]['dijk_path'][0]))
        """
            "paths": {
                "trajectory": [viewpoint_id1, viewpoint_id2, ..., ],
                "action": [act_1, act_2, ..., ],
                "listener_scores": [log_prob_act1, log_prob_act2, ..., ],
                "visual_feature": [(f1_step1, f2_step2, ...), (f1_step2, f2_step2, ...)
            }
        """
        # Gather the Path
        for i, result in enumerate(results):
            assert len(finished[i]) <= args.candidates
            for state_id in finished[i]:
                path_info = {
                    "trajectory": [],
                    "action": [],
                    "listener_scores": id2state[i][state_id]['scores'],
                    "listener_actions": id2state[i][state_id]['actions'],
                    "visual_feature": []
                }
                viewpoint, action = decompose_state_id(state_id)
                while action != -95:
                    state = id2state[i][state_id]
                    path_info['trajectory'].append(state['location'])
                    path_info['action'].append(action)
                    path_info['visual_feature'].append(state['feature'])
                    state_id = id2state[i][state_id]['from_state_id']
                    viewpoint, action = decompose_state_id(state_id)
                state = id2state[i][state_id]
                path_info['trajectory'].append(state['location'])
                for need_reverse_key in ["trajectory", "action", "visual_feature"]:
                    path_info[need_reverse_key] = path_info[need_reverse_key][::-1]
                result['paths'].append(path_info)

        return results

    def beam_search(self, speaker):
        """
        :param speaker: The speaker to be used in searching.
        :return:
        {
            "scan": XXX
            "instr_id":XXX,
            "instr_encoding": XXX
            "dijk_path": [v1, v2, ...., vn]
            "paths": [{
                "trajectory": [viewoint_id0, viewpoint_id1, viewpoint_id2, ..., ],
                "action": [act_1, act_2, ..., ],
                "listener_scores": [log_prob_act1, log_prob_act2, ..., ],
                "speaker_scores": [log_prob_word1, log_prob_word2, ..., ],
            }]
        }
        """
        self.env.reset()
        results = self._dijkstra()
        """
        return from self._dijkstra()
        [{
            "scan": XXX
            "instr_id":XXX,
            "instr_encoding": XXX
            "dijk_path": [v1, v2, ...., vn]
            "paths": [{
                    "trajectory": [viewoint_id0, viewpoint_id1, viewpoint_id2, ..., ],
                    "action": [act_1, act_2, ..., ],
                    "listener_scores": [log_prob_act1, log_prob_act2, ..., ],
                    "visual_feature": [(f1_step1, f2_step2, ...), (f1_step2, f2_step2, ...)
            }]
        }]
        """

        # Compute the speaker scores:
        for result in results:
            lengths = []
            num_paths = len(result['paths'])
            for path in result['paths']:
                assert len(path['trajectory']) == (len(path['visual_feature']) + 1)
                lengths.append(len(path['visual_feature']))
            max_len = max(lengths)
            img_feats = torch.zeros(num_paths, max_len, 36, self.feature_size + args.angle_feat_size)
            can_feats = torch.zeros(num_paths, max_len, self.feature_size + args.angle_feat_size)
            for j, path in enumerate(result['paths']):
                for k, feat in enumerate(path['visual_feature']):
                    img_feat, can_feat = feat
                    img_feats[j][k] = img_feat
                    can_feats[j][k] = can_feat
            img_feats, can_feats = img_feats.cuda(), can_feats.cuda()
            features = ((img_feats, can_feats), lengths)
            insts = np.array([result['instr_encoding'] for _ in range(num_paths)])
            seq_lengths = np.argmax(insts == self.tok.word_to_index['<EOS>'], axis=1)   # len(seq + 'BOS') == len(seq + 'EOS')
            insts = torch.from_numpy(insts).cuda()
            speaker_scores = speaker.teacher_forcing(train=True, features=features, insts=insts, for_listener=True)
            for j, path in enumerate(result['paths']):
                path.pop("visual_feature")
                path['speaker_scores'] = -speaker_scores[j].detach().cpu().numpy()[:seq_lengths[j]]
        return results

    def beam_search_test(self, speaker):
        self.encoder.eval()
        self.decoder.eval()
        self.critic.eval()

        looped = False
        self.results = {}
        while True:
            for traj in self.beam_search(speaker):
                if traj['instr_id'] in self.results:
                    looped = True
                else:
                    self.results[traj['instr_id']] = traj
            if looped:
                break

    def test(self, use_dropout=False, feedback='argmax', allow_cheat=False, iters=None):
        ''' Evaluate once on each instruction in the current environment '''
        self.feedback = feedback
        if use_dropout:
            self.encoder.train()
            self.decoder.train()
            self.critic.train()
        else:
            self.encoder.eval()
            self.decoder.eval()
            self.critic.eval()
        super(Seq2SeqAgent, self).test(iters)

    def zero_grad(self):
        self.loss = 0.
        self.losses = []
        for model, optimizer in zip(self.models, self.optimizers):
            model.train()
            optimizer.zero_grad()

    def accumulate_gradient(self, feedback='teacher', **kwargs):
        if feedback == 'teacher':
            self.feedback = 'teacher'
            self.rollout(train_ml=args.teacher_weight, train_rl=False, **kwargs)
        elif feedback == 'sample':
            self.feedback = 'teacher'
            self.rollout(train_ml=args.ml_weight, train_rl=False, **kwargs)
            self.feedback = 'sample'
            self.rollout(train_ml=None, train_rl=True, **kwargs)
        else:
            assert False

    def optim_step(self):
        self.loss.backward()

        torch.nn.utils.clip_grad_norm(self.encoder.parameters(), 40.)
        torch.nn.utils.clip_grad_norm(self.decoder.parameters(), 40.)

        self.encoder_optimizer.step()
        self.decoder_optimizer.step()
        self.critic_optimizer.step()

    def train(self, n_iters, feedback='teacher', **kwargs):
        ''' Train for a given number of iterations '''
        self.feedback = feedback

        self.encoder.train()
        self.decoder.train()
        self.critic.train()

        self.losses = []
        for iter in range(1, n_iters + 1):

            self.encoder_optimizer.zero_grad()
            self.decoder_optimizer.zero_grad()
            self.critic_optimizer.zero_grad()

            self.loss = 0
            if feedback == 'teacher':
                self.feedback = 'teacher'
                self.rollout(train_ml=args.teacher_weight, train_rl=False, **kwargs)
            elif feedback == 'sample':
                if args.ml_weight != 0:
                    self.feedback = 'teacher'
                    self.rollout(train_ml=args.ml_weight, train_rl=False, **kwargs)
                self.feedback = 'sample'
                self.rollout(train_ml=None, train_rl=True, **kwargs)
            else:
                assert False

            self.loss.backward()

            torch.nn.utils.clip_grad_norm(self.encoder.parameters(), 40.)
            torch.nn.utils.clip_grad_norm(self.decoder.parameters(), 40.)

            self.encoder_optimizer.step()
            self.decoder_optimizer.step()
            self.critic_optimizer.step()

    def save(self, epoch, path):
        ''' Snapshot models '''
        the_dir, _ = os.path.split(path)
        os.makedirs(the_dir, exist_ok=True)
        states = {}
        def create_state(name, model, optimizer):
            states[name] = {
                'epoch': epoch + 1,
                'state_dict': model.state_dict(),
                'optimizer': optimizer.state_dict(),
            }
        all_tuple = [("encoder", self.encoder, self.encoder_optimizer),
                     ("decoder", self.decoder, self.decoder_optimizer),
                     ("critic", self.critic, self.critic_optimizer)]
        for param in all_tuple:
            create_state(*param)
        torch.save(states, path)

    def load(self, path):
        ''' Loads parameters (but not training state) '''
        states = torch.load(path)
        def recover_state(name, model, optimizer):
            state = model.state_dict()
            model_keys = set(state.keys())
            load_keys = set(states[name]['state_dict'].keys())
            if model_keys != load_keys:
                print("NOTICE: DIFFERENT KEYS IN THE LISTEREN")
            state.update(states[name]['state_dict'])
            model.load_state_dict(state)
            if args.loadOptim:
                optimizer.load_state_dict(states[name]['optimizer'])
        all_tuple = [("encoder", self.encoder, self.encoder_optimizer),
                     ("decoder", self.decoder, self.decoder_optimizer),
                     ("critic", self.critic, self.critic_optimizer)]
        for param in all_tuple:
            recover_state(*param)
        return states['encoder']['epoch'] - 1
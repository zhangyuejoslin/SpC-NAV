import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.nn.functional as F
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
from param import args
from modules import build_mlp, SoftAttention, PositionalEncoding, ScaledDotProductAttention, create_mask, create_mask_for_object, proj_masking, StateAttention, ConfigObjAttention
#from transformers import LxmertTokenizer, LxmertModel

class ObjEncoder(nn.Module):
    ''' Encodes object labels using GloVe. '''

    def __init__(self, vocab_size, embedding_size, glove_matrix):
        super(ObjEncoder, self).__init__()

        padding_idx = 100
        word_embeds = nn.Embedding(vocab_size, embedding_size, padding_idx)
        word_embeds.load_state_dict({'weight': glove_matrix})
        self.embedding = word_embeds
        self.embedding.weight.requires_grad = False

    def forward(self, inputs):
        embeds = self.embedding(inputs)
        return embeds

class EncoderLSTM(nn.Module):
    ''' Encodes navigation instructions, returning hidden state context (for
        attention methods) and a decoder initial state. '''

    def __init__(self, vocab_size, embedding_size, hidden_size, padding_idx, 
                            dropout_ratio, bidirectional=False, num_layers=1):
        super(EncoderLSTM, self).__init__()
        self.embedding_size = embedding_size
        self.hidden_size = hidden_size
        self.drop = nn.Dropout(p=dropout_ratio)
        if bidirectional:
            print("Using Bidir in EncoderLSTM")
        self.num_directions = 2 if bidirectional else 1
        self.num_layers = num_layers
        self.embedding = nn.Embedding(vocab_size, embedding_size, padding_idx)
        input_size = embedding_size
        self.lstm = nn.LSTM(input_size, hidden_size, self.num_layers,
                            batch_first=True, dropout=dropout_ratio, 
                            bidirectional=bidirectional)
        self.encoder2decoder = nn.Linear(hidden_size * self.num_directions,
            hidden_size * self.num_directions
        )

    def init_state(self, inputs):
        ''' Initialize to zero cell states and hidden states.'''
        batch_size = inputs.size(0)
        h0 = Variable(torch.zeros(
            self.num_layers * self.num_directions,
            batch_size,
            self.hidden_size
        ), requires_grad=False)
        c0 = Variable(torch.zeros(
            self.num_layers * self.num_directions,
            batch_size,
            self.hidden_size
        ), requires_grad=False)

        return h0.cuda(), c0.cuda()

    def forward(self, inputs, lengths):
        ''' Expects input vocab indices as (batch, seq_len). Also requires a 
            list of lengths for dynamic batching. '''
        embeds = self.embedding(inputs)  # (batch, seq_len, embedding_size)
        embeds = self.drop(embeds)
        h0, c0 = self.init_state(inputs)
        packed_embeds = pack_padded_sequence(embeds, lengths, batch_first=True)
        enc_h, (enc_h_t, enc_c_t) = self.lstm(packed_embeds, (h0, c0))

        if self.num_directions == 2:    # The size of enc_h_t is (num_layers * num_directions, batch, hidden_size)
            h_t = torch.cat((enc_h_t[-1], enc_h_t[-2]), 1)
            c_t = torch.cat((enc_c_t[-1], enc_c_t[-2]), 1)
        else:
            h_t = enc_h_t[-1]
            c_t = enc_c_t[-1] # (batch, hidden_size)

        ctx, _ = pad_packed_sequence(enc_h, batch_first=True)

        if args.sub_out == "max":
            ctx_max, _ = ctx.max(1)
            decoder_init = nn.Tanh()(self.encoder2decoder(ctx_max))
        elif args.sub_out == "tanh":
            decoder_init = nn.Tanh()(self.encoder2decoder(h_t))
        else:
            assert False

        ctx = self.drop(ctx)
        if args.zero_init:
            return ctx, torch.zeros_like(decoder_init), torch.zeros_like(c_t)
        else:
            return ctx, decoder_init, c_t  # (batch, seq_len, hidden_size*num_directions)
                                 # (batch, hidden_size)


class SoftDotAttention(nn.Module):
    '''Soft Dot Attention. 
    Ref: http://www.aclweb.org/anthology/D15-1166
    Adapted from PyTorch OPEN NMT.
    '''

    def __init__(self, query_dim, ctx_dim):
        '''Initialize layer.'''
        super(SoftDotAttention, self).__init__()
        self.linear_in = nn.Linear(query_dim, ctx_dim, bias=False)
        self.sm = nn.Softmax()
        self.linear_out = nn.Linear(query_dim + ctx_dim, query_dim, bias=False)
        self.tanh = nn.Tanh()

    def forward(self, h, context, mask=None,
                output_tilde=True, output_prob=True):
        '''Propagate h through the network.
        h: batch x dim
        context: batch x seq_len x dim
        mask: batch x seq_len indices to be masked
        '''
        target = self.linear_in(h).unsqueeze(2)  # batch x dim x 1

        # Get attention
        attn = torch.bmm(context, target).squeeze(2)  # batch x seq_len
        logit = attn

        if mask is not None:
            # -Inf masking prior to the softmax
            attn.masked_fill_(mask, -float('inf'))
        attn = self.sm(attn)    # There will be a bug here, but it's actually a problem in torch source code.
        attn3 = attn.view(attn.size(0), 1, attn.size(1))  # batch x 1 x seq_len

        weighted_context = torch.bmm(attn3, context).squeeze(1)  # batch x dim
        if not output_prob:
            attn = logit
        if output_tilde:
            h_tilde = torch.cat((weighted_context, h), 1)
            h_tilde = self.tanh(self.linear_out(h_tilde))
            return h_tilde, attn
        else:
            return weighted_context, attn


class AttnDecoderLSTM(nn.Module):
    ''' An unrolled LSTM with attention over instructions for decoding navigation actions. '''

    def __init__(self, embedding_size, hidden_size,
                       dropout_ratio, feature_size=2048+4):
        super(AttnDecoderLSTM, self).__init__()
        self.embedding_size = embedding_size
        self.feature_size = feature_size
        self.hidden_size = hidden_size
        self.embedding = nn.Sequential(
            nn.Linear(args.angle_feat_size, self.embedding_size),
            nn.Tanh()
        )
        self.drop = nn.Dropout(p=dropout_ratio)
        self.drop_env = nn.Dropout(p=args.featdropout)
        self.lstm = nn.LSTMCell(embedding_size+feature_size, hidden_size)
        self.feat_att_layer = SoftDotAttention(hidden_size, feature_size)
        self.attention_layer = SoftDotAttention(hidden_size, hidden_size)
        self.candidate_att_layer = SoftDotAttention(hidden_size, feature_size)
        self.LMmodel = LxmertModel.from_pretrained('unc-nlp/lxmert-base-uncased', cache_dir='/VL/space/zhan1624/lxmert-base-uncased')
        

    def forward(self, action, feature, cand_feat,
                h_0, prev_h1, c_0,
                ctx, ctx_mask=None,
                already_dropfeat=False):
        '''
        Takes a single step in the decoder LSTM (allowing sampling).
        action: batch x angle_feat_size
        feature: batch x 36 x (feature_size + angle_feat_size)
        cand_feat: batch x cand x (feature_size + angle_feat_size)
        h_0: batch x hidden_size
        prev_h1: batch x hidden_size
        c_0: batch x hidden_size
        ctx: batch x seq_len x dim
        ctx_mask: batch x seq_len - indices to be masked
        already_dropfeat: used in EnvDrop
        '''
        action_embeds = self.embedding(action)

        # Adding Dropout
        action_embeds = self.drop(action_embeds)

        if not already_dropfeat:
            # Dropout the raw feature as a common regularization
            feature[..., :-args.angle_feat_size] = self.drop_env(feature[..., :-args.angle_feat_size])   # Do not drop the last args.angle_feat_size (position feat)

        prev_h1_drop = self.drop(prev_h1)
        attn_feat, _ = self.feat_att_layer(prev_h1_drop, feature, output_tilde=False)

        concat_input = torch.cat((action_embeds, attn_feat), 1) # (batch, embedding_size+feature_size)
        h_1, c_1 = self.lstm(concat_input, (prev_h1, c_0))

        h_1_drop = self.drop(h_1)
        h_tilde, alpha = self.attention_layer(h_1_drop, ctx, ctx_mask)

        # Adding Dropout
        h_tilde_drop = self.drop(h_tilde)

        if not already_dropfeat:
            cand_feat[..., :-args.angle_feat_size] = self.drop_env(cand_feat[..., :-args.angle_feat_size])

        _, logit = self.candidate_att_layer(h_tilde_drop, cand_feat, output_prob=False)

        return h_1, c_1, logit, h_tilde


class Critic(nn.Module):
    def __init__(self):
        super(Critic, self).__init__()
        self.state2value = nn.Sequential(
            nn.Linear(args.rnn_dim, args.rnn_dim),
            nn.ReLU(),
            nn.Dropout(args.dropout),
            nn.Linear(args.rnn_dim, 1),
        )

    def forward(self, state):
        return self.state2value(state).squeeze()

class SpeakerEncoder(nn.Module):
    def __init__(self, feature_size, hidden_size, dropout_ratio, bidirectional):
        super().__init__()
        self.num_directions = 2 if bidirectional else 1
        self.hidden_size = hidden_size
        self.num_layers = 1
        self.feature_size = feature_size

        if bidirectional:
            print("BIDIR in speaker encoder!!")

        self.lstm = nn.LSTM(feature_size, self.hidden_size // self.num_directions, self.num_layers,
                            batch_first=True, dropout=dropout_ratio, bidirectional=bidirectional)
        self.drop = nn.Dropout(p=dropout_ratio)
        self.drop3 = nn.Dropout(p=args.featdropout)
        self.attention_layer = SoftDotAttention(self.hidden_size, feature_size)

        self.post_lstm = nn.LSTM(self.hidden_size, self.hidden_size // self.num_directions, self.num_layers,
                                 batch_first=True, dropout=dropout_ratio, bidirectional=bidirectional)

    def forward(self, action_embeds, feature, lengths, already_dropfeat=False):
        """
        :param action_embeds: (batch_size, length, 2052). The feature of the view
        :param feature: (batch_size, length, 36, 2052). The action taken (with the image feature)
        :param lengths: Not used in it
        :return: context with shape (batch_size, length, hidden_size)
        """
        x = action_embeds
        if not already_dropfeat:
            x[..., :-args.angle_feat_size] = self.drop3(x[..., :-args.angle_feat_size])            # Do not dropout the spatial features

        # LSTM on the action embed
        ctx, _ = self.lstm(x)
        ctx = self.drop(ctx)

        # Att and Handle with the shape
        batch_size, max_length, _ = ctx.size()
        if not already_dropfeat:
            feature[..., :-args.angle_feat_size] = self.drop3(feature[..., :-args.angle_feat_size])   # Dropout the image feature
        x, _ = self.attention_layer(                        # Attend to the feature map
            ctx.contiguous().view(-1, self.hidden_size),    # (batch, length, hidden) --> (batch x length, hidden)
            feature.view(batch_size * max_length, -1, self.feature_size),        # (batch, length, # of images, feature_size) --> (batch x length, # of images, feature_size)
        )
        x = x.view(batch_size, max_length, -1)
        x = self.drop(x)

        # Post LSTM layer
        x, _ = self.post_lstm(x)
        x = self.drop(x)

        return x

class SpeakerDecoder(nn.Module):
    def __init__(self, vocab_size, embedding_size, padding_idx, hidden_size, dropout_ratio):
        super().__init__()
        self.hidden_size = hidden_size
        self.embedding = torch.nn.Embedding(vocab_size, embedding_size, padding_idx)
        self.lstm = nn.LSTM(embedding_size, hidden_size, batch_first=True)
        self.drop = nn.Dropout(dropout_ratio)
        self.attention_layer = SoftDotAttention(hidden_size, hidden_size)
        self.projection = nn.Linear(hidden_size, vocab_size)
        self.baseline_projection = nn.Sequential(
            nn.Linear(hidden_size, 128),
            nn.ReLU(),
            nn.Dropout(dropout_ratio),
            nn.Linear(128, 1)
        )

    def forward(self, words, ctx, ctx_mask, h0, c0):
        embeds = self.embedding(words)
        embeds = self.drop(embeds)
        x, (h1, c1) = self.lstm(embeds, (h0, c0))

        x = self.drop(x)

        # Get the size
        batchXlength = words.size(0) * words.size(1)
        multiplier = batchXlength // ctx.size(0)         # By using this, it also supports the beam-search

        # Att and Handle with the shape
        # Reshaping x          <the output> --> (b(word)*l(word), r)
        # Expand the ctx from  (b, a, r)    --> (b(word)*l(word), a, r)
        # Expand the ctx_mask  (b, a)       --> (b(word)*l(word), a)
        x, _ = self.attention_layer(
            x.contiguous().view(batchXlength, self.hidden_size),
            ctx.unsqueeze(1).expand(-1, multiplier, -1, -1).contiguous(). view(batchXlength, -1, self.hidden_size),
            mask=ctx_mask.unsqueeze(1).expand(-1, multiplier, -1).contiguous().view(batchXlength, -1)
        )
        x = x.view(words.size(0), words.size(1), self.hidden_size)

        # Output the prediction logit
        x = self.drop(x)
        logit = self.projection(x)

        return logit, h1, c1


class ConfiguringObject(nn.Module):

    def __init__(self, img_fc_dim, img_fc_use_batchnorm, img_dropout, img_feat_input_dim,
                 rnn_hidden_size, rnn_dropout, max_len, fc_bias=True, max_navigable=16):
        super(ConfiguringObject, self).__init__()
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.max_navigable = max_navigable
        self.feature_size = img_feat_input_dim
        self.hidden_size = rnn_hidden_size

        proj_navigable_img_kwargs = {
             #add 36 if add similarity
            'input_dim': img_feat_input_dim,
            'hidden_dims': img_fc_dim,
            'use_batchnorm': img_fc_use_batchnorm,
            'dropout': img_dropout,
            'fc_bias': fc_bias,
            'relu': 1
        }
        self.proj_navigable_img_mlp = build_mlp(**proj_navigable_img_kwargs)

        proj_navigable_obj_kwargs1 = {
            'input_dim': 152, #152
            'hidden_dims': img_fc_dim,
            'use_batchnorm': img_fc_use_batchnorm,
            'dropout': img_dropout,
            'fc_bias': fc_bias,
            'relu': 1
        }
        self.proj_navigable_obj_mlp1 = build_mlp(**proj_navigable_obj_kwargs1)
        
       
        proj_navigable_img_kwargs2 = {
             #add 36 if add similarity
            'input_dim': img_feat_input_dim+36,
            'hidden_dims': img_fc_dim,
            'use_batchnorm': img_fc_use_batchnorm,
            'dropout': img_dropout,
            'fc_bias': fc_bias,
            'relu': 1
        }
        self.proj_navigable_img_mlp2 = build_mlp(**proj_navigable_img_kwargs2)


        self.h0_fc = nn.Linear(rnn_hidden_size, img_fc_dim[-1], bias=False)
        self.next_h0_fc = nn.Linear(256, 128, bias=False)

        self.soft_attn = SoftAttention()
        self.state_attention = StateAttention()

        self.config_obj_attention = ConfigObjAttention()

        self.dropout = nn.Dropout(p=rnn_dropout)
        
        self.lstm = nn.LSTMCell(img_fc_dim[-1] + rnn_hidden_size + 300 + 300 , rnn_hidden_size)
        #self.lstm = nn.LSTMCell(img_fc_dim[-1] + rnn_hidden_size, rnn_hidden_size)


        self.h1_fc = nn.Linear(rnn_hidden_size, rnn_hidden_size, bias=False)

        self.h2_fc_lstm = nn.Linear(rnn_hidden_size + img_fc_dim[-1], rnn_hidden_size, bias=fc_bias)

        self.proj_out = nn.Linear(rnn_hidden_size, img_fc_dim[-1], bias=fc_bias)

        self.logit_fc = nn.Linear(rnn_hidden_size * 2 + 300 + 300, img_fc_dim[-1])
        #self.logit_fc = nn.Linear(rnn_hidden_size*2, img_fc_dim[-1])

        self.r_linear = nn.Linear(rnn_hidden_size + 128, 2)

        self.image_linear = nn.Linear(img_feat_input_dim, img_fc_dim[-1])

        self.config_fc = nn.Linear(512+300+300, 128, bias=False)
        #self.config_fc = nn.Linear(512, 128, bias=False)

        self.config_atten_linear = nn.Linear(512, 128)
        #self.config_atten_linear = nn.Linear(768, 128)

        self.sm = nn.Softmax(dim=1)

        self.drop_env = nn.Dropout(p=args.featdropout)

        self.r_transform = Variable(torch.tensor([[1,0,0.75,0.5],[0,1,0.25,0.5]]).transpose(0,1), requires_grad=False)
        self.ho_trans = nn.Linear(768, 512)
        self.h0_next = nn.Linear(rnn_hidden_size, img_fc_dim[-1], bias=False)


    def forward(self, navigable_img_feat, navigable_obj_feat, navigable_obj_img_feat, object_mask, pre_feat, h_0, c_0, ctx, 
                s_0, r_t, navigable_index, ctx_mask, step, landmark_similarity):

        """ Takes a single step in the decoder LSTM.
        config_embedding: batch x max_config_len x config embeddding
        image_feature: batch x 12 images x 36 boxes x image_feature_size
        navigable_index: list of navigable viewstates
        h_t: batch x hidden_size
        c_t: batch x hidden_size
        ctx_mask: batch x seq_len - indices to be masked
        """
        # input of image_feature should be changed
    
        
        batch_size, num_heading, num_object, object_feat_dim = navigable_obj_feat.size()

        # object text feature
        navigable_obj_feat = navigable_obj_feat.view(batch_size, num_heading*num_object, object_feat_dim) #4 x 16*36 x 300 
        
        # object image feature
        navigable_obj_img_feat = navigable_obj_img_feat.view(batch_size, num_heading*num_object, 152) # 4 x 48*36 x 152
        index_length = [len(_index)+1 for _index in navigable_index]
        
        navigable_mask = create_mask(batch_size, int(num_heading/3), index_length)
        
        # not add similarity
        proj_navigable_obj_feat = proj_masking(navigable_obj_img_feat, self.proj_navigable_obj_mlp1, object_mask.view(batch_size, num_heading*num_object)) # batch x 48*36 x 152 -> batch x 48*36 x 128
        #proj_navigable_feat = proj_masking(navigable_img_feat, self.proj_navigable_img_mlp, navigable_mask.repeat(1,3))
        
        
        # add similarity with two methods
        proj_navigable_feat = proj_masking(torch.cat([navigable_img_feat, torch.sort(landmark_similarity, dim=-1)[0]],2), self.proj_navigable_img_mlp2, navigable_mask.repeat(1,3)) # batch x 48 x 128
        #proj_navigable_feat = proj_masking(torch.cat([navigable_img_feat, landmark_similarity],2), self.proj_navigable_img_mlp, navigable_mask.repeat(1,3))
        # landmark_similarity: 4 x 48 x 36
        # navigable_img_feat: 4 x 48 x 2176  
                                                                             
        #proj_pre_feat = self.proj_navigable_img_mlp(pre_feat)

        weighted_img_feat, img_attn = self.soft_attn(self.h0_fc(h_0), proj_navigable_feat, mask=navigable_mask.repeat(1,3))

        if r_t is None:
            r_t = self.r_linear(torch.cat((weighted_img_feat, h_0), dim=1))
            r_t = self.sm(r_t)
        
    
        weighted_ctx, ctx_attn = self.state_attention(s_0, r_t, ctx, ctx_mask, step)

        conf_obj_feat, conf_obj_attn = self.config_obj_attention(self.config_fc(weighted_ctx), proj_navigable_obj_feat, navigable_mask, object_mask) # 4 x 16 x 128
        weighted_conf_obj_feat, conf_obj_attn = self.soft_attn(self.h0_fc(h_0), conf_obj_feat, mask=navigable_mask.repeat(1,3)) # 4 x 128

        new_weighted_img_feat = torch.bmm(conf_obj_attn.unsqueeze(dim=1), self.image_linear(navigable_img_feat)).squeeze(dim=1)# batch x 128
        
        #concat_input = torch.cat((proj_pre_feat, new_weighted_img_feat, weighted_ctx), 1)
        concat_input = torch.cat((new_weighted_img_feat, weighted_ctx), 1)

        h_1, c_1 = self.lstm(concat_input, (h_0, c_0))
        h_1_drop = self.dropout(h_1)

        # policy network
        h_tilde = self.logit_fc(torch.cat((weighted_ctx, h_1_drop), dim=1))
        logit = torch.bmm(proj_navigable_feat, h_tilde.unsqueeze(2)).squeeze(2)
        logit = logit[:,0:int(num_heading/3)] + logit[:,int(num_heading/3):2*int(num_heading/3)] + logit[:,2*int(num_heading/3):num_heading]
    
        return h_1, c_1, ctx_attn, logit

class ConfiguringRelationObject(nn.Module):

    def __init__(self, img_fc_dim, img_fc_use_batchnorm, img_dropout, img_feat_input_dim,
                 rnn_hidden_size, rnn_dropout, max_len, fc_bias=True, max_navigable=16):
        super(ConfiguringRelationObject, self).__init__()
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.max_navigable = max_navigable
        self.feature_size = img_feat_input_dim
        self.hidden_size = rnn_hidden_size

        proj_navigable_img_kwargs = {
             #add 36 if add similarity
            'input_dim': img_feat_input_dim,
            'hidden_dims': img_fc_dim,
            'use_batchnorm': img_fc_use_batchnorm,
            'dropout': img_dropout,
            'fc_bias': fc_bias,
            'relu': 1
        }
        self.proj_navigable_img_mlp = build_mlp(**proj_navigable_img_kwargs)

        proj_navigable_obj_kwargs1 = {
            'input_dim': 152, #152
            'hidden_dims': img_fc_dim,
            'use_batchnorm': img_fc_use_batchnorm,
            'dropout': img_dropout,
            'fc_bias': fc_bias,
            'relu': 1
        }
        self.proj_navigable_obj_mlp1 = build_mlp(**proj_navigable_obj_kwargs1)
        
       
        proj_navigable_img_kwargs2 = {
             #add 36 if add similarity
            'input_dim': img_feat_input_dim+36+36+1,
            'hidden_dims': img_fc_dim,
            'use_batchnorm': img_fc_use_batchnorm,
            'dropout': img_dropout,
            'fc_bias': fc_bias,
            'relu': 1
        }
        self.proj_navigable_img_mlp2 = build_mlp(**proj_navigable_img_kwargs2)


        self.h0_fc = nn.Linear(rnn_hidden_size, img_fc_dim[-1], bias=False)

        self.soft_attn = SoftAttention()
        self.state_attention = StateAttention()

        self.config_obj_attention = ConfigObjAttention()

        self.dropout = nn.Dropout(p=rnn_dropout)
        
        self.lstm = nn.LSTMCell(img_fc_dim[-1] + rnn_hidden_size + 300 + 300 , rnn_hidden_size)

        #self.lstm = nn.LSTMCell(img_fc_dim[-1] * 2 + rnn_hidden_size, rnn_hidden_size)


        self.h1_fc = nn.Linear(rnn_hidden_size, rnn_hidden_size, bias=False)

        self.proj_out = nn.Linear(rnn_hidden_size, img_fc_dim[-1], bias=fc_bias)

        self.logit_fc = nn.Linear(rnn_hidden_size * 2 + 300 + 300, img_fc_dim[-1])
        #self.logit_fc = nn.Linear(rnn_hidden_size * 2, img_fc_dim[-1])

        self.r_linear = nn.Linear(rnn_hidden_size + 128, 2)

        self.image_linear = nn.Linear(img_feat_input_dim, img_fc_dim[-1])

        self.config_fc = nn.Linear(512+300+300, 128, bias=False)
        #self.config_fc = nn.Linear(512, 128, bias=False)

        self.config_atten_linear = nn.Linear(512, 128)
        #self.config_atten_linear = nn.Linear(768, 128)

        self.sm = nn.Softmax(dim=1)

        self.drop_env = nn.Dropout(p=args.featdropout)

        # self.triplet_arg1 = nn.Linear(300, 100, bias=False)
        # self.triplet_arg2 = nn.Linear(300, 100, bias=False)
        # self.triplet_arg3 = nn.Linear(300, 100, bias=False)

        #self.obj_rel_arg1 = nn.Linear(152, 100, bias=False)
        self.obj_rel_arg2 = nn.Linear(12, 100, bias=False) # obj_relation
        #self.obj_rel_arg3 = nn.Linear(152, 100, bias=False)

        self.view_arg = nn.Linear(4, 100, bias=False)


    def forward(self, navigable_img_feat, navigable_obj_feat, navigable_obj_img_feat, object_mask, pre_feat, h_0, c_0, ctx, 
                s_0, r_t, navigable_index, ctx_mask, step, triplet_tensor, candidate_obj_relation_feat, obj_rel_arg_feat, img_view_feat, view_tensor, landmark_similarity=None):

        """ Takes a single step in the decoder LSTM.
        config_embedding: batch x max_config_len x config embeddding
        image_feature: batch x 12 images x 36 boxes x image_feature_size
        navigable_index: list of navigable viewstates
        h_t: batch x hidden_size
        c_t: batch x hidden_size
        ctx_mask: batch x seq_len - indices to be masked
        """
        # input of image_feature should be changed
    
        
        #batch_size, num_heading, num_object, obj_feat_dim = navigable_obj_feat.size()
        batch_size, num_heading, num_object, obj_feat_dim = navigable_obj_img_feat.size()
        config_num = ctx.shape[1]
        triplet_num = triplet_tensor.shape[2]
        view_num = view_tensor.shape[2]
        triplet_dim = 100
        view_dim = 100

        # object text feature
        #navigable_obj_feat = navigable_obj_feat.view(batch_size, num_heading*num_object, obj_feat_dim) #4 x 16*36 x 300 
        
        # object image feature
        navigable_obj_img_feat = navigable_obj_img_feat.view(batch_size, num_heading*num_object, 152) # 4 x 48*36 x 152
        
        # navigable_obj_img_feat = self.drop_env(navigable_obj_img_feat)
        # navigable_img_feat[..., :-args.angle_feat_size] = self.drop_env(navigable_img_feat[..., :-args.angle_feat_size])

        index_length = [len(_index)+1 for _index in navigable_index]
        
        navigable_mask = create_mask(batch_size, int(num_heading/3), index_length)


        # triplet_feat = torch.cat((self.triplet_arg1(triplet_tensor[:,:,:,0]), self.triplet_arg2(triplet_tensor[:,:,:,1]), self.triplet_arg3(triplet_tensor[:,:,:,2])), dim=-1).view(batch_size, -1, triplet_dim*3)
        # tmp_obj_feat = torch.cat((self.obj_rel_arg1(obj_rel_arg_feat[:,:,:,:,:obj_feat_dim]), self.obj_rel_arg2(candidate_obj_relation_feat), self.obj_rel_arg3(obj_rel_arg_feat[:,:,:,:,obj_feat_dim:2*obj_feat_dim])), dim=-1).view(batch_size, num_heading*num_object*num_object, triplet_dim*3)
        # weighted_obj_rel_feat = torch.mean(torch.bmm(tmp_obj_feat, triplet_feat.transpose(1,2)).view(batch_size, num_heading, num_object, num_object*config_num*triplet_num), dim=-1)
    
        #triplet similarity
        triplet_feat = triplet_tensor.view(batch_size, -1, triplet_dim*3)
        tmp_obj_feat = torch.cat((obj_rel_arg_feat[:,:,:,:,:300], self.obj_rel_arg2(candidate_obj_relation_feat), obj_rel_arg_feat[:,:,:,:,300:2*300]), dim=-1).view(batch_size, num_heading*num_object*num_object, triplet_dim*3)
        obj_rel_sim = torch.mean(torch.bmm(tmp_obj_feat, triplet_feat.transpose(1,2)).view(batch_size, num_heading, num_object, num_object*config_num*triplet_num), dim=-1)

        #view similarity
        view_feat = view_tensor.view(batch_size, -1, view_dim)
        view_sim = torch.mean(torch.bmm(self.view_arg(img_view_feat), view_feat.transpose(1,2)).view(batch_size, int(num_heading/3), config_num*view_num), dim=-1).repeat(1,3)

        # not add similarity
        proj_navigable_obj_feat = proj_masking(navigable_obj_img_feat, self.proj_navigable_obj_mlp1, object_mask.view(batch_size, num_heading*num_object)) # batch x 48*36 x 152 -> batch x 48*36 x 128
        #proj_navigable_feat = proj_masking(navigable_img_feat, self.proj_navigable_img_mlp, navigable_mask.repeat(1,3))
        
        # add similarity with two methods
        proj_navigable_feat = proj_masking(torch.cat([navigable_img_feat, landmark_similarity, torch.sort(obj_rel_sim, dim=-1)[0], torch.sort(view_sim.unsqueeze(-1), dim=-1)[0]],2),  self.proj_navigable_img_mlp2, navigable_mask.repeat(1,3)) # batch x 48 x 128
        #proj_navigable_feat = proj_masking(torch.cat([navigable_img_feat, landmark_similarity],2), self.proj_navigable_img_mlp, navigable_mask.repeat(1,3))
        # landmark_similarity: 4 x 48 x 36
        # navigable_img_feat: 4 x 48 x 2176  
                                                                             
        #proj_pre_feat = self.proj_navigable_img_mlp(pre_feat)

        weighted_img_feat, img_attn = self.soft_attn(self.h0_fc(h_0), proj_navigable_feat, mask=navigable_mask.repeat(1,3))

        if r_t is None:
            r_t = self.r_linear(torch.cat((weighted_img_feat, h_0), dim=1))
            r_t = self.sm(r_t)
        
    
        weighted_ctx, ctx_attn = self.state_attention(s_0, r_t, ctx, ctx_mask, step)

        conf_obj_feat, conf_obj_attn = self.config_obj_attention(self.config_fc(weighted_ctx), proj_navigable_obj_feat, navigable_mask, object_mask) # 4 x 16 x 128
        weighted_conf_obj_feat, conf_obj_attn = self.soft_attn(self.h0_fc(h_0), conf_obj_feat, mask=navigable_mask.repeat(1,3)) # 4 x 128

        new_weighted_img_feat = torch.bmm(conf_obj_attn.unsqueeze(dim=1), self.image_linear(navigable_img_feat)).squeeze(dim=1)# batch x 128
        
        #concat_input = torch.cat((proj_pre_feat, new_weighted_img_feat, weighted_ctx), 1)
        concat_input = torch.cat((new_weighted_img_feat, weighted_ctx), 1)

        h_1, c_1 = self.lstm(concat_input, (h_0, c_0))
        h_1_drop = self.dropout(h_1)

        # policy network
        h_tilde = self.logit_fc(torch.cat((weighted_ctx, h_1_drop), dim=1))
        logit = torch.bmm(proj_navigable_feat, h_tilde.unsqueeze(2)).squeeze(2)
        logit = logit[:,0:int(num_heading/3)] + logit[:,int(num_heading/3):2*int(num_heading/3)] + logit[:,2*int(num_heading/3):num_heading]
    
        return h_1, c_1, ctx_attn, logit


class ConfigurationDecoder(nn.Module):
    def __init__(self, embedding_size, hidden_size,
                    dropout_ratio, feature_size=2048+4):
        super(ConfigurationDecoder, self).__init__()
        self.embedding_size = embedding_size
        self.feature_size = feature_size
        self.hidden_size = hidden_size
        self.embedding = nn.Sequential(
            nn.Linear(args.angle_feat_size, self.embedding_size),
            nn.Tanh()
        )
        self.drop = nn.Dropout(p=dropout_ratio)
        self.drop_env = nn.Dropout(p=args.featdropout)
        self.lstm = nn.LSTMCell(embedding_size+feature_size, hidden_size)
        self.feat_att_layer = SoftDotAttention(hidden_size, feature_size)
        self.attention_layer = SoftDotAttention(hidden_size, hidden_size)
        self.candidate_att_layer = SoftDotAttention(hidden_size, feature_size)
        self.similarity_att_layer = SoftDotAttention(hidden_size, hidden_size)
        self.object_att_layer = SoftDotAttention(hidden_size, hidden_size)
        self.state_attention = StateAttention()
        self.r_linear = nn.Linear(self.hidden_size, 2)
        self.sm = nn.Softmax(dim=-1)

        self.weight_linear = nn.Linear(2, 1)
        self.config_obj_attention = ConfigObjAttention()
        self.proj_object = nn.Linear(152, hidden_size)
        self.object_text_linear = nn.Linear(300,2048)
        self.text_relation_linear = nn.Linear(300,300)
        self.obj_relation_linear = nn.Linear(12,300)

        self.view_img_linear = nn.Linear(4, 300)
        self.view_text_linear = nn.Linear(300,300)

        self.similarity_linear = nn.Linear(36,512)


    def forward(self, action, feature, cand_feat,
                h_0, prev_h1, c_0,
                ctx, step, s_0, r_t, ctx_mask, object_mask=None, landmark_object_feature = None, candidate_obj_img_feat = None, candidate_obj_text_feat = None, landmark_mask=None,
                landmark_triplet_feature=None, obj_rel_arg_feat=None, candidat_relation=None,
                view_img_feat=None, view_feature=None, view_object_similarity=None, view_mask=None,
                already_dropfeat=False):
        '''
        Takes a single step in the decoder LSTM (allowing sampling).
        action: batch x angle_feat_size
        feature: batch x 36 x (feature_size + angle_feat_size)
        cand_feat: batch x cand x (feature_size + angle_feat_size)
        h_0: batch x hidden_size
        prev_h1: batch x hidden_size
        c_0: batch x hidden_size
        ctx: batch x seq_len x dim
        ctx_mask: batch x seq_len - indices to be masked
        already_dropfeat: used in EnvDrop
        '''

        #object text similarity
  
        batch_size, image_num, object_num, obj_feat_dim = candidate_obj_text_feat.shape
        max_config_num = ctx.shape[1]
        landmark_similarity = torch.matmul(candidate_obj_text_feat, torch.transpose(landmark_object_feature.view(batch_size, -1, 300).unsqueeze(1),3,2))# (4*48*36*300) * (4*1*300*180???-> 4 * 48 * 36*180
        #landmark_similarity = torch.max(landmark_similarity.view(batch_size, image_num, object_num, max_config_num, -1),dim=-1)[0]# 4 * 48 * 36 * 15 * 12
        landmark_similarity = torch.mean(landmark_similarity.view(batch_size, image_num, object_num, max_config_num, -1),dim=-1)
       
        #object image similarity
        '''
        batch_size, image_num, object_num, obj_feat_dim = candidate_obj_text_feat.shape
        max_config_num = ctx.shape[1]
        landmark_similarity = torch.matmul(candidate_obj_img_feat, torch.transpose(self.object_text_linear(landmark_object_feature).view(batch_size, -1, 2048).unsqueeze(1),3,2))# (4*48*36*300) * (4*1*300*180???-> 4 * 48 * 36*180
        landmark_similarity = torch.max(landmark_similarity.view(batch_size, image_num, object_num, max_config_num, -1),dim=-1)[0]# 4 * 48 * 36 * 15 * 12
        '''
        action_embeds = self.embedding(action)
    
        # Adding Dropout
        action_embeds = self.drop(action_embeds)

        config_num = ctx.shape[1]
        if not already_dropfeat:
            # Dropout the raw feature as a common regularization
            feature[..., :-args.angle_feat_size] = self.drop_env(feature[..., :-args.angle_feat_size])   # Do not drop the last args.angle_feat_size (position feat)

        prev_h1_drop = self.drop(prev_h1)
        attn_feat, _ = self.feat_att_layer(prev_h1_drop, feature, output_tilde=False)

        concat_input = torch.cat((action_embeds, attn_feat), 1) # (batch, embedding_size+feature_size)
        h_1, c_1 = self.lstm(concat_input, (prev_h1, c_0))

        h_1_drop = self.drop(h_1)

        # if r_t is None:
        #     r_t = self.r_linear(h_1_drop)
        #     r_t = self.sm(r_t)
        
        # h_tilde, ctx_attn = self.state_attention(s_0, r_t, ctx, ctx_mask, step)
        h_tilde, ctx_attn = self.attention_layer(h_1_drop, ctx, ctx_mask)

        # Adding Dropout
        h_tilde_drop = self.drop(h_tilde)
        
        if not already_dropfeat:
            cand_feat[..., :-args.angle_feat_size] = self.drop_env(cand_feat[..., :-args.angle_feat_size])
        '''
        if landmark_similarity is not None:
            # object relation similarity
            arg_sim1 = torch.matmul(obj_rel_arg_feat[:,:,:,:,:300], torch.transpose(landmark_triplet_feature[:,:,:,0].view(batch_size,1,1,-1,300),3,4))
            arg_sim2 = torch.matmul(obj_rel_arg_feat[:,:,:,:,300:], torch.transpose(landmark_triplet_feature[:,:,:,2].view(batch_size,1,1,-1,300),3,4))
            text_relation = self.text_relation_linear(landmark_triplet_feature[:,:,:,1])
            obj_relation = self.obj_relation_linear(candidat_relation)
            arg_sim3 = torch.matmul(obj_relation, torch.transpose(text_relation.view(batch_size,1,1,-1,300),3,4))
            arg_mean = torch.mean(torch.stack([arg_sim1, arg_sim2, arg_sim3], dim=-1), dim=-1)
            arg_mean = torch.mean(arg_mean.view(batch_size, image_num, 36,36, config_num,-1),dim=-1)
            weighted_arg_mean = torch.matmul(arg_mean, torch.transpose(ctx_attn.reshape(ctx_attn.shape[0],1,1,1,ctx_attn.shape[1]),4,3)).squeeze(-1)
            weighted_arg_mean = torch.mean(weighted_arg_mean.view(batch_size,image_num,-1), dim=-1)
            # object similarity
            weighted_landmark_similarity = torch.max(torch.matmul(landmark_similarity, torch.transpose(ctx_attn.reshape(ctx_attn.shape[0],1,1,ctx_attn.shape[1]),3,2)).squeeze(-1),dim=-1)
            
            # view similarity
            view_arg_sim1 = torch.mean(torch.mean(view_object_similarity, dim=-1),dim=2)
            view_arg_sim2 = torch.mean(torch.matmul(self.view_img_linear(view_img_feat), torch.transpose(self.view_text_linear(view_feature)[:,:,:,2].view(batch_size,-1,300),2,1)).view(batch_size,image_num,config_num,-1), dim=-1)
        
            weighted_view_sim = torch.matmul(torch.mean(torch.stack([view_arg_sim1, view_arg_sim2], dim=2),dim=2), torch.transpose(ctx_attn.reshape(ctx_attn.shape[0],1,ctx_attn.shape[1]),2,1)).squeeze(-1)
            
        

            
            #_, logit1 = self.similarity_att_layer(h_tilde_drop, self.similarity_linear(weighted_landmark_similarity), output_prob=False)
        '''
        if view_object_similarity is not None:
            view_arg_sim1 = torch.max(torch.mean(view_object_similarity, dim=-1),dim=2)[0]
            view_arg_sim2 = torch.max(torch.matmul(self.view_img_linear(view_img_feat), torch.transpose(self.view_text_linear(view_feature)[:,:,:,2].view(batch_size,-1,300),2,1)).view(batch_size,image_num,config_num,-1), dim=-1)[0]
            weighted_view_sim = torch.matmul(torch.mean(torch.stack([view_arg_sim1, view_arg_sim2], dim=2),dim=2), torch.transpose(ctx_attn.reshape(ctx_attn.shape[0],1,ctx_attn.shape[1]),2,1)).squeeze(-1)
            logit_view = weighted_view_sim*weighted_view_sim 

        weighted_landmark_similarity = torch.max(torch.matmul(landmark_similarity, torch.transpose(ctx_attn.reshape(ctx_attn.shape[0],1,1,ctx_attn.shape[1]),3,2)).squeeze(-1)*object_mask, dim=-1)[0]
        #weighted_landmark_similarity = torch.mean(torch.sort(torch.matmul(landmark_similarity, torch.transpose(ctx_attn.reshape(ctx_attn.shape[0],1,1,ctx_attn.shape[1]),3,2)).squeeze(-1)*object_mask, dim=-1, descending=True)[0][:,:,:10],dim=-1)
        

        _, logit_original = self.candidate_att_layer(h_tilde_drop, cand_feat, output_prob=False)
        
        logit = self.weight_linear(torch.stack([weighted_landmark_similarity, logit_original],dim=-1)).squeeze(-1)

        #logit = torch.mean(torch.stack([self.sm(weighted_landmark_similarity), logit_original],dim=1), dim=1)
        
        #logit = logit_original
        
        '''
        logit = torch.zeros(batch_size, image_num).cuda()

        if view_object_similarity is not None:
            tmp_logit_original = torch.mean(torch.stack([logit_sim, logit_original],dim=1), dim=1)
            logit_sim = torch.mean(torch.stack([logit_sim, logit_original, logit_view],dim=1), dim=1)
            logit[torch.where(view_mask == 0)] = tmp_logit_original[torch.where(view_mask == 0)]
            logit[torch.where(view_mask == 1)] = logit_sim[torch.where(view_mask == 1)]

        
        else:
            logit_sim = torch.mean(torch.stack([logit_sim, logit_original],dim=1), dim=1)
            logit[torch.where(landmark_mask == 0)] = logit_original[torch.where(landmark_mask == 0)]
            logit[torch.where(landmark_mask == 1)] = logit_sim[torch.where(landmark_mask == 1)]
        
        '''
        

        return h_1, c_1, logit, h_tilde, ctx_attn




class ConfigurationLXMERTDecoder(nn.Module):
    def __init__(self, embedding_size, hidden_size,
                    dropout_ratio, feature_size=2048+4):
        super(ConfigurationLXMERTDecoder, self).__init__()
        self.embedding_size = embedding_size
        self.feature_size = feature_size
        self.hidden_size = hidden_size
        self.embedding = nn.Sequential(
            nn.Linear(args.angle_feat_size, self.embedding_size),
            nn.Tanh()
        )
        self.drop = nn.Dropout(p=dropout_ratio)
        self.drop_env = nn.Dropout(p=args.featdropout)
        self.lstm = nn.LSTMCell(embedding_size+feature_size, hidden_size)
        self.feat_att_layer = SoftDotAttention(hidden_size, feature_size)
        self.attention_layer = SoftDotAttention(hidden_size, hidden_size)
        self.candidate_att_layer = SoftDotAttention(hidden_size, feature_size)
        self.similarity_att_layer = SoftDotAttention(hidden_size, hidden_size)
        self.object_att_layer = SoftDotAttention(hidden_size, hidden_size)
        self.state_attention = StateAttention()
        self.r_linear = nn.Linear(self.hidden_size, 2)
        self.sm = nn.Softmax(dim=-1)

        self.weight_linear = nn.Linear(2, 1)
        self.config_obj_attention = ConfigObjAttention()
        self.proj_object = nn.Linear(152, hidden_size)
        self.object_text_linear = nn.Linear(300,2048)
        self.text_relation_linear = nn.Linear(300,300)
        self.obj_relation_linear = nn.Linear(12,300)

        self.view_img_linear = nn.Linear(4, 300)
        self.view_text_linear = nn.Linear(300,300)

    def forward(self, action, feature, cand_feat,
                h_0, prev_h1, c_0,
                ctx, step, s_0, r_t, ctx_mask, object_mask=None, landmark_object_feature = None, candidate_obj_img_feat = None, landmark_similarity=None, candidate_obj_text_feat = None, landmark_mask=None,
                landmark_triplet_feature=None, obj_rel_arg_feat=None, candidat_relation=None,
                view_img_feat=None, view_feature=None, view_object_similarity=None, view_mask=None,
                already_dropfeat=False):
        '''
        Takes a single step in the decoder LSTM (allowing sampling).
        action: batch x angle_feat_size
        feature: batch x 36 x (feature_size + angle_feat_size)
        cand_feat: batch x cand x (feature_size + angle_feat_size)
        h_0: batch x hidden_size
        prev_h1: batch x hidden_size
        c_0: batch x hidden_size
        ctx: batch x seq_len x dim
        ctx_mask: batch x seq_len - indices to be masked
        already_dropfeat: used in EnvDrop
        '''
        batch_size, image_num, object_num, obj_feat_dim = candidate_obj_text_feat.shape
        max_config_num = ctx.shape[1]
        #landmark_similarity = torch.matmul(candidate_obj_text_feat, torch.transpose(landmark_object_feature.view(batch_size, -1, 300).unsqueeze(1),3,2))# (4*48*36*300) * (4*1*300*180???-> 4 * 48 * 36*180
        landmark_similarity = torch.max(landmark_similarity.view(batch_size, image_num, object_num, max_config_num, -1),dim=-1)[0]# 4 * 48 * 36 * 15 * 12
       
        #object image similarity
        action_embeds = self.embedding(action)
    
        # Adding Dropout
        action_embeds = self.drop(action_embeds)

        config_num = ctx.shape[1]
        if not already_dropfeat:
            # Dropout the raw feature as a common regularization
            feature[..., :-args.angle_feat_size] = self.drop_env(feature[..., :-args.angle_feat_size])   # Do not drop the last args.angle_feat_size (position feat)

        prev_h1_drop = self.drop(prev_h1)
        attn_feat, _ = self.feat_att_layer(prev_h1_drop, feature, output_tilde=False)

        concat_input = torch.cat((action_embeds, attn_feat), 1) # (batch, embedding_size+feature_size)
        h_1, c_1 = self.lstm(concat_input, (prev_h1, c_0))

        h_1_drop = self.drop(h_1)

        h_tilde, ctx_attn = self.attention_layer(h_1_drop, ctx, ctx_mask)

        # Adding Dropout
        h_tilde_drop = self.drop(h_tilde)
        
        if not already_dropfeat:
            cand_feat[..., :-args.angle_feat_size] = self.drop_env(cand_feat[..., :-args.angle_feat_size])

        weighted_landmark_similarity = torch.mean(torch.matmul(landmark_similarity, torch.transpose(ctx_attn.reshape(ctx_attn.shape[0],1,1,ctx_attn.shape[1]),3,2)).squeeze(-1)*object_mask, dim=-1)

        _, logit_original = self.candidate_att_layer(h_tilde_drop, cand_feat, output_prob=False)
        logit = self.weight_linear(torch.stack([weighted_landmark_similarity, logit_original],dim=-1)).squeeze(-1)
        
        return h_1, c_1, logit, h_tilde, ctx_attn
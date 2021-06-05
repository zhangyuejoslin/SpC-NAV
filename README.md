# Code and Data for Paper "Towards Navigation by Reasoning over Spatial Configurations" 

## Environment Installation
Download Room-to-Room navigation data:
```
bash ./tasks/R2R/data/download.sh
```

Download image features for environments:
```
mkdir img_features
wget https://www.dropbox.com/s/o57kxh2mn5rkx4o/ResNet-152-imagenet.zip -P img_features/
cd img_features
unzip ResNet-152-imagenet.zip
```

Python requirements: Need python3.6 (python 3.5 should be OK since I removed the allennlp dependencies)
```
pip install -r python_requirements.txt
```

Install Matterport3D simulators:
```
git submodule update --init --recursive 
sudo apt-get install libjsoncpp-dev libepoxy-dev libglm-dev libosmesa6 libosmesa6-dev libglew-dev
mkdir build && cd build
cmake -DEGL_RENDERING=ON ..
make -j8
```


## Code

### Speaker
```
bash run/speaker.bash 0
```
0 is the id of GPU. It will train the speaker and save the snapshot under snap/speaker/

### Agent
```
bash run/agent.bash 0
```
0 is the id of GPU. It will train the agent and save the snapshot under snap/agent/. Unseen success rate would be around 46%.

### Agent + Speaker (Back Translation)
After pre-training the speaker and the agnet,
```
bash run/bt_envdrop.bash 0
```
0 is the id of GPU. 
It will load the pre-trained agent and run back translation with environmental dropout.

### Spatial Configuration, Motion Indicator, Landmark Features
https://drive.google.com/file/d/1Mf2yiP4mdTUwSn6r9aNiCjcsqLsoYRHM/view?usp=sharing






# Code and Data for Paper "Towards Navigation by Reasoning over Spatial Configurations" 
Our paper is accepted in SpLU workshop of ACL2021.

## Environment Installation
Flease follow https://github.com/airsplay/R2R-EnvDrop to set up the environment.



## Run Agent
```
bash run/agent.bash 0
```
0 is the id of GPU. It will train the agent and save the snapshot under snap/agent/. Unseen success rate would be around 46%.

```
bash run/bt_envdrop.bash 0
```
0 is the id of GPU. 
It will load the pre-trained agent and run back translation with environmental dropout.

## Spatial Configuration, Motion Indicator, Landmark Features
Modify the parameters in param.py to the corresponding path, and the following link is the corresponding features.
https://drive.google.com/file/d/1Mf2yiP4mdTUwSn6r9aNiCjcsqLsoYRHM/view?usp=sharing






#!/usr/bin/env python3
from __future__ import annotations
import argparse,gzip,json,struct,time
from pathlib import Path
import numpy as np
import mindspore
from mindspore import Tensor,nn
from mindspore.dataset import GeneratorDataset

def images(path):
 with gzip.open(path,'rb') as f:
  magic,n,r,c=struct.unpack('>IIII',f.read(16))
  if magic!=2051 or (r,c)!=(28,28): raise ValueError('bad images')
  return np.frombuffer(f.read(),dtype=np.uint8).reshape(n,784).astype(np.float16)/np.float16(255)
def labels(path):
 with gzip.open(path,'rb') as f:
  magic,n=struct.unpack('>II',f.read(8)); x=np.frombuffer(f.read(),dtype=np.uint8)
 if magic!=2049 or len(x)!=n: raise ValueError('bad labels')
 return x.astype(np.int32)
class EdgeLeNet(nn.Cell):
 def __init__(self):
  super().__init__(); self.layers=nn.SequentialCell([nn.Dense(784,64),nn.ReLU(),nn.Dense(64,32),nn.ReLU(),nn.Dense(32,10)])
 def construct(self,x): return self.layers(x)
def cast(net):
 for p in net.get_parameters(): p.set_dtype(mindspore.float16)
def dataset(x,y,batch):
 labels_onehot=np.eye(10,dtype=np.float16)[y]
 def rows():
  for i in range(len(x)): yield x[i], labels_onehot[i]
 return GeneratorDataset(rows,['image','label'],shuffle=True,num_parallel_workers=1,python_multiprocessing=False).batch(batch,drop_remainder=False)
def load(path):
 net=EdgeLeNet(); cast(net); missing,unused=mindspore.load_param_into_net(net,mindspore.load_checkpoint(str(path)))
 if missing or unused: raise RuntimeError(f'checkpoint mismatch:{missing};{unused}')
 net.set_train(False); return net
def evaluate(net,x,y):
 pred=[]
 for i in range(0,len(x),64): pred.extend(net(Tensor(x[i:i+64])).asnumpy().argmax(1).astype(int).tolist())
 return float(np.mean(np.asarray(pred)==y)),pred
def infer(out,net,x,y):
 x,y=x[:6],y[:6]; pred=net(Tensor(x)).asnumpy().argmax(1).astype(int).tolist(); result={'checkpoint':str(out/'lenet-final.ckpt'),'checkpoint_loaded':True,'predicted':pred,'labels':y.astype(int).tolist(),'figure':'inference.png','mindspore_version':mindspore.__version__,'device_target':'Ascend','device_id':0}; (out/'inference-result.json').write_text(json.dumps(result,indent=2)+'\n'); import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt; fig,axes=plt.subplots(2,3,figsize=(9,6)); [ (ax.imshow(x[i].reshape(28,28),cmap='gray'),ax.set_title(f'predicted={pred[i]}, label={int(y[i])}'),ax.axis('off')) for i,ax in enumerate(axes.flat) ]; fig.tight_layout(); fig.savefig(out/'inference.png',dpi=140); plt.close(fig); return result
def main():
 p=argparse.ArgumentParser(); p.add_argument('--epochs',type=int,default=10); p.add_argument('--limit',type=int,default=256); p.add_argument('--batch-size',type=int,default=128); p.add_argument('--data',default='/var/lib/smart-textbook/mindspore/mnist'); p.add_argument('--output',default='/home/student/lab_reference_mindspore/measured-lenet'); p.add_argument('--action',choices=['train','evaluate','infer'],default='train'); a=p.parse_args()
 if not 1<=a.epochs<=100 or not 6<=a.limit<=60000: raise SystemExit('invalid parameters')
 cache=Path.cwd()/'kernel_meta'; cache.mkdir(parents=True, exist_ok=True); mindspore.set_context(mode=mindspore.GRAPH_MODE,device_target='Ascend',device_id=0,ascend_config={'precision_mode':'allow_mix_precision'}); root=Path(a.data); tx=images(root/'train-images-idx3-ubyte.gz')[:a.limit]; ty=labels(root/'train-labels-idx1-ubyte.gz')[:len(tx)]; vx=images(root/'t10k-images-idx3-ubyte.gz')[:max(6,min(1000,a.limit))]; vy=labels(root/'t10k-labels-idx1-ubyte.gz')[:len(vx)]; out=Path(a.output); out.mkdir(parents=True,exist_ok=True); ck=out/'lenet-final.ckpt'
 if a.action=='train':
  net=EdgeLeNet(); cast(net); model=mindspore.Model(net,loss_fn=nn.MSELoss(),optimizer=nn.SGD(net.trainable_params(),learning_rate=0.01)); t=time.monotonic(); model.train(a.epochs,dataset(tx,ty,a.batch_size),dataset_sink_mode=False); seconds=time.monotonic()-t; mindspore.save_checkpoint(net,str(ck)); net=load(ck); acc,_=evaluate(net,vx,vy); report={'epochs':a.epochs,'training_seconds':seconds,'metrics':{'accuracy':acc},'mindspore_version':mindspore.__version__,'device_target':'Ascend','device_id':0,'train_samples':len(tx),'test_samples':len(vx),'optimizer':'SGD','loss':'MSE(one-hot-FP16)'}; (out/'measured-result.json').write_text(json.dumps(report,indent=2)+'\n'); print(json.dumps({**report,'inference':infer(out,net,vx,vy)}),flush=True)
 elif a.action=='evaluate':
  net=load(ck); acc,_=evaluate(net,vx,vy); report={'accuracy':acc,'checkpoint':str(ck),'device_target':'Ascend'}; (out/'measured-result.json').write_text(json.dumps(report,indent=2)+'\n'); print(json.dumps(report),flush=True)
 else: print(json.dumps(infer(out,load(ck),vx,vy)),flush=True)
if __name__=='__main__': main()

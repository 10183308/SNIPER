import os
os.environ['PYTHONUNBUFFERED'] = '1'
os.environ['MXNET_CUDNN_AUTOTUNE_DEFAULT'] = '0'
os.environ['MXNET_ENABLE_GPU_P2P'] = '0'
import init
from iterators.MNIteratorFaster import MNIteratorFaster
from load_model import load_param
import sys
sys.path.insert(0,'lib')
from symbols.faster.resnet_v1_50_fast import resnet_v1_50_fast,checkpoint_callback
from configs.faster.default_configs import config,update_config,get_opt_params
import mxnet as mx
import metric,callback
import numpy as np
from general_utils import get_optim_params,get_fixed_param_names,create_logger

from iterators.PrefetchingIter import PrefetchingIter
from load_data import load_proposal_roidb,merge_roidb,filter_roidb
from bbox.bbox_regression import add_bbox_regression_targets
from argparse import ArgumentParser

def parser():
	arg_parser = ArgumentParser('Faster R-CNN training module')
	arg_parser.add_argument('--cfg',dest='cfg',help='Path to the config file',
                        default='configs/faster/res50_coco.yml',type=str) 
	arg_parser.add_argument('--display',dest='display',help='Number of epochs between displaying loss info',
                        default=100,type=int) 
	arg_parser.add_argument('--save_prefix',dest='save_prefix',help='Prefix used for snapshotting the network',
                        default='CRCNN',type=str) 

	return arg_parser.parse_args()

if __name__=='__main__':
	args = parser()
	update_config(args.cfg)
	context=[mx.gpu(int(gpu)) for gpu in config.gpus.split(',')]
	nGPUs = len(context)
	batch_size = nGPUs * config.TRAIN.BATCH_IMAGES
	
	if not os.path.isdir(config.output_path):
		os.mkdir(config.output_path)


	# Create roidb
	image_sets = [iset for iset in config.dataset.image_set.split('+')]
	roidbs = [load_proposal_roidb(config.dataset.dataset, image_set, config.dataset.root_path, config.dataset.dataset_path,
								  proposal=config.dataset.proposal, append_gt=True, flip=True, result_path=config.output_path)
			  for image_set in image_sets]
	roidb = merge_roidb(roidbs)
	roidb = filter_roidb(roidb, config)
	bbox_means, bbox_stds = add_bbox_regression_targets(roidb, config)

	# Creating the iterator
	print('Creating Iterator with {} Images'.format(len(roidb)))
	train_iter = MNIteratorFaster(roidb=roidb,config=config,batch_size=batch_size,nGPUs=nGPUs,threads=batch_size)

	# Creating the module
	print('Initializing the model...')
	sym_inst = resnet_v1_50_fast()
	sym = sym_inst.get_symbol_rcnn(config)
	
	# Creating the Logger
	logger, output_path = create_logger(config.output_path, args.cfg, config.dataset.image_set)

	# get list of fixed parameters
	fixed_param_names = get_fixed_param_names(config.network.FIXED_PARAMS,sym)

	# Creating the module
	mod = mx.mod.Module(symbol=sym,
					context=context,
					data_names=[k[0] for k in train_iter.provide_data_single],
					label_names=[k[0] for k in train_iter.provide_label_single],
					fixed_param_names=fixed_param_names)
	shape_dict = dict(train_iter.provide_data_single+train_iter.provide_label_single)
	sym_inst.infer_shape(shape_dict)
	arg_params, aux_params = load_param(config.network.pretrained,config.network.pretrained_epoch,convert=True)
	sym_inst.init_weight_rcnn(config,arg_params,aux_params)


	# Creating the metrics
	eval_metric = metric.RCNNAccMetric(config)
	cls_metric  = metric.RCNNLogLossMetric(config)
	bbox_metric = metric.RCNNL1LossMetric(config)
	eval_metrics = mx.metric.CompositeEvalMetric()
	eval_metrics.add(eval_metric)
	eval_metrics.add(cls_metric)
	eval_metrics.add(bbox_metric) 


	eval_metrics = mx.metric.CompositeEvalMetric()
	eval_metrics.add(eval_metric)
	eval_metrics.add(cls_metric)
	eval_metrics.add(bbox_metric)

	optimizer_params = get_optim_params(config,len(roidb),batch_size)
	print ('Optimizer params: {}'.format(optimizer_params))

	# Checkpointing
	prefix = os.path.join(output_path,args.save_prefix)
	batch_end_callback = mx.callback.Speedometer(batch_size, args.display)
	epoch_end_callback = [mx.callback.module_checkpoint(mod, prefix, period=1, save_optimizer_states=True),
		checkpoint_callback(sym_inst.get_bbox_param_names(),prefix, bbox_means, bbox_stds)]

	train_iter = PrefetchingIter(train_iter)
	mod.fit(train_iter,optimizer='sgd',optimizer_params=optimizer_params,
			eval_metric=eval_metrics,num_epoch=config.TRAIN.end_epoch,kvstore=config.default.kvstore,
			batch_end_callback=batch_end_callback,
			epoch_end_callback=epoch_end_callback, arg_params=arg_params,aux_params=aux_params)
	
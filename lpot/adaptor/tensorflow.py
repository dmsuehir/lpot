#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2020 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import copy
from collections import OrderedDict
import yaml
import numpy as np
from .query import QueryBackendCapability
from .adaptor import adaptor_registry, Adaptor
from ..utils.utility import LazyImport, CpuInfo, singleton
from ..utils import logger
from ..conf.dotdict import deep_get
tensorflow = LazyImport('tensorflow')


@adaptor_registry
class TensorFlowAdaptor(Adaptor):
    unify_op_type_mapping = {
        "Conv2D": "conv2d",
        "DepthwiseConv2dNative": "conv2d",
        "MaxPool": "pooling",
        "AvgPool": "pooling",
        "ConcatV2": "concat",
        "MatMul": "matmul",
        "Pad": "pad"
    }

    def __init__(self, framework_specific_info):
        super(TensorFlowAdaptor, self).__init__(framework_specific_info)

        self.quantize_config = {'op_wise_config': {}}
        self.framework_specific_info = framework_specific_info
        self.device = self.framework_specific_info['device']
        self.work_dir = os.path.abspath(self.framework_specific_info['workspace_path'])
        self.pre_optimized_graph = None
        self.pre_optimizer_handle = None
        self.input_tensor_names = self.framework_specific_info['inputs'] \
            if 'inputs' in self.framework_specific_info else []
        self.output_tensor_names = self.framework_specific_info['outputs'] \
            if 'outputs' in self.framework_specific_info else []
        self.output_node_names = list(
            set([x.split(":")[0] for x in self.output_tensor_names]))
        self.input_node_names = list(
            set([x.split(":")[0] for x in self.input_tensor_names]))

        self.bf16_ops = []
        self.fp32_ops = []
        self.dump_times = 0   # for tensorboard
        self.query_handler = TensorflowQuery(local_config_file=os.path.join(
            os.path.dirname(__file__), "tensorflow.yaml"))
        self.op_wise_sequences = self.query_handler.get_eightbit_patterns()

    def log_histogram(self, writer, tag, values, step=0, bins=1000):
        import tensorflow as tf
        # Convert to a numpy array
        values = np.array(values)

        # Create histogram using numpy
        counts, bin_edges = np.histogram(values, bins=bins)

        # Fill fields of histogram proto
        hist = tf.compat.v1.HistogramProto()
        hist.min = float(np.min(values))
        hist.max = float(np.max(values))
        hist.num = int(np.prod(values.shape))
        hist.sum = float(np.sum(values))
        hist.sum_squares = float(np.sum(values**2))

        bin_edges = bin_edges[1:]

        for edge in bin_edges:
            hist.bucket_limit.append(edge)
        for c in counts:
            hist.bucket.append(c)

        # Create and write Summary
        summary = tf.compat.v1.Summary(value=[tf.compat.v1.Summary.Value(tag=tag, histo=hist)])
        writer.add_summary(summary, step)
        writer.flush()

    def _dequantize(self, data, scale_info):
        original_shape = data.shape
        size = data.size
        new_data = data.reshape(size, )
        max_value = 255 if scale_info[0].find("Relu") != -1 else 127
        return np.array([float(i / max_value) for i in new_data]).reshape(original_shape)

    def evaluate(self, input_graph, dataloader, postprocess=None,
                 metric=None, measurer=None, iteration=-1, tensorboard=False):
        """Evaluate the model for specified metric on validation dataset.

        Args:
            input_graph ([Graph, GraphDef or Path String]): The model could be the graph,
                        graph_def object, the frozen pb or ckpt/savedmodel folder path.
            dataloader (generator): generate the data and labels.
            postprocess (object, optional): process the result from the model
            metric (object, optional): Depends on model category. Defaults to None.
            measurer (object, optional): for precise benchmark measurement.
            iteration(int, optional): control steps of mini-batch
            tensorboard (boolean, optional): for tensorboard inspect tensor.

        Returns:
            [float]: evaluation result, the larger is better.
        """
        import tensorflow as tf
        from .tf_utils.util import get_graph_def
        from .tf_utils.util import get_tensor_by_name, iterator_sess_run

        graph = tf.Graph()
        graph_def = get_graph_def(input_graph, self.output_node_names)
        self._validate_and_inference_input_output(graph_def)
        outputs = copy.deepcopy(self.output_tensor_names)

        assert graph_def
        with graph.as_default():
            tf.import_graph_def(graph_def, name='')

        iter_op = None
        if 'MakeIterator' in [node.op for node in graph_def.node]:
            iter_op = graph.get_operation_by_name('MakeIterator')

        if tensorboard:
            from .tf_utils.graph_rewriter.graph_util import GraphAnalyzer
            from tensorflow.python.framework import tensor_util

            output_postfix = "_fp32.output"
            inspect_node_types = ["Conv2D", "DepthwiseConv2dNative", "MaxPool", "AvgPool",
                                  "ConcatV2", "MatMul", "FusedBatchNormV3", "BiasAdd",
                                  "Relu", "Relu6", "Dequantize"]
            fp32_inspect_node_name = []
            int8_inspect_node_name = []
            q_node_scale = {}
            if self.dump_times == 0:
                temp_dir = "./runs/eval/baseline"
            else:
                temp_dir = "./runs/eval/tune_" + str(self.dump_times)
            if os.path.isdir(temp_dir):
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)
            writer = tf.compat.v1.summary.FileWriter(temp_dir, graph)

            cur_graph = GraphAnalyzer()
            cur_graph.graph = graph_def
            cur_graph.parse_graph()
            graph_info = cur_graph.node_name_details
            for node in graph_def.node:
                if node.op in inspect_node_types:
                    fp32_inspect_node_name.append(node.name)
                # Tensor dump supported quantized op including,
                # Requantize, QuantizedConv2DAndRequantize,
                # QuantizedConv2DAndReluAndRequantize,
                # QuantizedConv2DWithBiasAndRequantize,
                # QuantizedConv2DWithBiasAndReluAndRequantize,
                # QuantizedConv2DWithBiasSignedSumAndReluAndRequantize,
                # QuantizedConv2DWithBiasSumAndReluAndRequantize,
                # QuantizedDepthwiseConv2DWithBiasAndReluAndRequantize,
                # QuantizedMatMulWithBiasAndReluAndRequantize,
                # QuantizedMatMulWithBiasAndRequantize
                elif node.op.find("Requantize") != -1:
                    out_min = -2
                    out_max = -1
                    if node.op.find("Sum") != -1:
                        out_min = -5
                        out_max = -4
                    q_out_min = graph_info[node.input[out_min]
                                           ].node.attr["value"].tensor.float_val[0]
                    q_out_max = graph_info[node.input[out_max]
                                           ].node.attr["value"].tensor.float_val[0]
                    q_node_scale[node.name] = (node.op, q_out_min, q_out_max)
                    int8_inspect_node_name.append(node.name)
                # Inspect weights, bias. Need further optimize
                if node.op == "Const" and graph_info[graph_info[node.name].outputs[0]].node.op \
                    in ["Conv2D", "DepthwiseConv2dNative", "MatMul",
                    "FusedBatchNormV3", "BiasAdd"]:
                    const_value = tensor_util.MakeNdarray(node.attr.get('value').tensor)
                    self.log_histogram(writer, node.name, const_value)

            outputs.extend(fp32_inspect_node_name)
            if len(int8_inspect_node_name) > 0:
                output_postfix = "_int8.output"
                outputs.extend(int8_inspect_node_name)

        input_tensor = [get_tensor_by_name(graph, x) for x in self.input_tensor_names]
        output_tensor = [get_tensor_by_name(graph, x) for x in outputs] if \
            len(outputs) > 1 else get_tensor_by_name(graph, outputs[0])

        config = tf.compat.v1.ConfigProto()
        config.use_per_session_threads = 1
        # config.intra_op_parallelism_threads = 28
        config.inter_op_parallelism_threads = 1
        sess = tf.compat.v1.Session(graph=graph, config=config)

        logger.info("Start to evaluate Tensorflow model...")
        for idx, (inputs, labels) in enumerate(dataloader):
            # dataloader should keep the order and len of inputs same with input_tensor
            if len(input_tensor) == 1:
                feed_dict = {input_tensor[0]: inputs}  # get raw tensor using index [0]
            else:
                assert len(input_tensor) == len(inputs), \
                    'inputs len must equal with input_tensor'
                feed_dict = dict(zip(input_tensor, inputs))

            if measurer is not None:
                measurer.start()
                predictions = sess.run(output_tensor, feed_dict) if iter_op is None \
                    else iterator_sess_run(sess, iter_op, feed_dict, output_tensor, iteration) 
                measurer.end()
            else:
                predictions = sess.run(output_tensor, feed_dict) if iter_op is None \
                    else iterator_sess_run(sess, iter_op, feed_dict, output_tensor, iteration) 

            # Inspect node output, just get 1st iteration output tensors for now
            if idx == 0 and tensorboard:
                for index, node_name in enumerate(outputs):
                    tensor = predictions[index]
                    if node_name in int8_inspect_node_name:
                        tensor = self._dequantize(predictions[index], q_node_scale[node_name])
                    self.log_histogram(writer, node_name + output_postfix, tensor, idx)
                writer.close()
            if isinstance(predictions, list):
                if len(self.output_tensor_names) == 1:
                    predictions = predictions[0]
                elif len(self.output_tensor_names) > 1:
                    predictions = predictions[:len(self.output_tensor_names)]
            if postprocess is not None:
                predictions, labels = postprocess((predictions, labels))
            if metric is not None:
                metric.update(predictions, labels)

            if idx + 1 == iteration:
                break
        acc = metric.result() if metric is not None else 0
        if tensorboard:
            new_dir = temp_dir + "_acc_" + str(acc)
            writer.close()
            if os.path.isdir(new_dir):
                import shutil
                shutil.rmtree(new_dir, ignore_errors=True)
            os.rename(temp_dir, new_dir)
            self.dump_times += 1
        sess.close()
        return acc

    def tuning_cfg_to_fw(self, tuning_cfg):
        """Parse the lpot wrapped configuration to Tensorflow.

        Args:
            tuning_cfg (dict): configuration for quantization.
        """
        self.quantize_config['calib_iteration'] = tuning_cfg['calib_iteration']
        self.quantize_config['device'] = self.device
        self.quantize_config['advance'] = deep_get(tuning_cfg, 'advance')
        fp32_ops = []
        bf16_ops = []
        for each_op_info in tuning_cfg['op']:
            op_name = each_op_info[0]

            if tuning_cfg['op'][each_op_info]['activation']['dtype'] in ['fp32', 'bf16']:
                if op_name in self.quantize_config['op_wise_config']:
                    self.quantize_config['op_wise_config'].pop(op_name)
                if tuning_cfg['op'][each_op_info]['activation']['dtype'] == 'fp32':
                    fp32_ops.append(op_name)
                if tuning_cfg['op'][each_op_info]['activation']['dtype'] == 'bf16':
                    bf16_ops.append(op_name)
                continue

            is_perchannel = False
            if 'weight' in tuning_cfg['op'][each_op_info]:
                is_perchannel = tuning_cfg['op'][each_op_info]['weight'][
                    'granularity'] == 'per_channel'
            algorithm = tuning_cfg['op'][each_op_info]['activation']['algorithm']

            is_asymmetric = False
            if 'activation' in tuning_cfg['op'][each_op_info]:
                is_asymmetric = tuning_cfg['op'][each_op_info]['activation']['scheme'] == 'asym'
            self.quantize_config['op_wise_config'][op_name] = (is_perchannel,
                                                               algorithm,
                                                               is_asymmetric)
        self.fp32_ops = fp32_ops
        self.bf16_ops = bf16_ops
        int8_sum_count = 0
        bf16_sum_count = 0
        log_length = 50
        logger.info('|' + 'Mixed Precision Statistics'.center(log_length, "*") + '|')
        for i in self._init_op_stat:
            if len(self._init_op_stat[i]) == 0:
                continue
            count = 0
            for j in self.quantize_config['op_wise_config'].keys():
                if j in self._init_op_stat[i]:
                    count += 1
            int8_sum_count += count
            logger.info('|' + 'INT8 {}: {} '.format(i, count).ljust(log_length) + '|')
            bf16_count = 0
            for k in self.bf16_ops:
                if k in self._init_op_stat[i]:
                    bf16_count += 1
                if bf16_count > 0:
                    logger.info(('|' + 'BF16 {}: {}'.format(i, bf16_count).ljust(log_length) \
                         + '|'))
            bf16_sum_count += bf16_count
        overall_ops_count = sum([len(v) for _, v in self._init_op_stat.items()])
        if overall_ops_count > 0:
            int8_percent = float(int8_sum_count / overall_ops_count)
            bf16_percent = float(bf16_sum_count / overall_ops_count)
            logger.info(('|' + 'Overall: INT8 {:.2%} ({}/{}) BF16 {:.2%} ({}/{})'.format(
                int8_percent,
                int8_sum_count,
                overall_ops_count,
                bf16_percent,
                bf16_sum_count,
                overall_ops_count)
                  .ljust(log_length) + '|'))
        logger.info('|' +  '*' * log_length + '|')

    def quantize(self, tune_cfg, model, data_loader, q_func=None):
        """Execute the quantize process on the specified model.

        Args:
            tune_cfg (dict): quantization configuration
            model (tf.compat.v1.GraphDef): fp32 model
            data_loader (generator): generator the data and labels
            q_func (optional): training function for quantization aware training mode,
                                which not enabled for tensorflow yet.

        Returns:
            tf.compat.v1.GraphDef: the quantized model
        """
        assert q_func is None, "quantization aware training mode is not support on tensorflow"
        logger.info('Start to run model quantization...')
        quantized_model = os.path.join(os.getcwd(), "tf_quantized.pb")
        self.tuning_cfg_to_fw(tune_cfg)
        logger.debug('Dump quantization configurations:')
        logger.debug(self.quantize_config)
        from .tf_utils.graph_converter import GraphConverter
        converter = GraphConverter(self.pre_optimized_graph if self.pre_optimized_graph else model,
                                   quantized_model,
                                   inputs=self.input_tensor_names,
                                   outputs=self.output_tensor_names,
                                   qt_config=self.quantize_config,
                                   int8_sequences=self.op_wise_sequences,
                                   fp32_ops=self.fp32_ops,
                                   bf16_ops=self.bf16_ops,
                                   data_loader=data_loader)

        return converter.convert()

    def _query_quantizable_ops(self, matched_nodes):
        """Collect the op-wise configuration for quantization.

        Returns:
            OrderDict: op-wise configuration.
        """
        uint8_type = self.query_handler.get_op_types_by_precision(precision='uint8')
        int8_type = self.query_handler.get_op_types_by_precision(precision='int8')
        tf_quantizable_op_type = list(set(uint8_type).union(set(int8_type)))

        valid_precision = self.query_handler.get_mixed_precision_combination()

        conv_config = self.query_handler.get_quantization_capability()['uint8']['Conv2D']
        matmul_config = self.query_handler.get_quantization_capability()['uint8']['MatMul']
        other_config = self.query_handler.get_quantization_capability()['uint8']['default']

        if ('bf16' in valid_precision and CpuInfo().bf16) or os.getenv('FORCE_BF16') == '1':
            conv_config['weight']['dtype'].append('bf16')
            matmul_config['weight']['dtype'].append('bf16')
            conv_config['activation']['dtype'].append('bf16')
            matmul_config['activation']['dtype'].append('bf16')
            other_config['activation']['dtype'].append('bf16')

        self.quantizable_op_details = OrderedDict()

        self._init_op_stat = {i: [] for i in tf_quantizable_op_type}
        for details in matched_nodes:
            node_op = details[-1][0]
            node_name = details[0]
            patterns = details[-1]
            pat_length = len(patterns)
            pattern_info = {
                'sequence': [[','.join(patterns[:pat_length - i]) for i in range(pat_length)][0]],
                'precision': ['int8']
            }
            if node_op in tf_quantizable_op_type and node_name not in self.exclude_node_names:
                self._init_op_stat[node_op].append(node_name)
                if self.unify_op_type_mapping[node_op].find("conv2d") != -1:
                    conv2d_int8_config = copy.deepcopy(conv_config)
                    conv2d_int8_config['pattern'] = pattern_info
                    self.quantizable_op_details[(
                        node_name, self.unify_op_type_mapping[node_op]
                    )] = conv2d_int8_config
                elif self.unify_op_type_mapping[node_op].find("matmul") != -1:
                    matmul_int8_config = copy.deepcopy(matmul_config)
                    matmul_int8_config['pattern'] = pattern_info
                    # TODO enable the sym mode once the tf fixed the mkldequantize_op.cc bug.
                    # is_positive_input = self.pre_optimizer_handle.has_positive_input(node_name)
                    # matmul_scheme = 'sym' if is_positive_input else 'asym'
                    matmul_scheme = ['asym']
                    matmul_int8_config['activation']['scheme'] = matmul_scheme
                    self.quantizable_op_details[(
                        node_name, self.unify_op_type_mapping[node_op]
                    )] = matmul_int8_config
                else:
                    self.quantizable_op_details[(
                        node_name, self.unify_op_type_mapping[node_op]
                    )] = copy.deepcopy(other_config)

                self.quantize_config['op_wise_config'][node_name] = (False, "minmax", False)
        return self.quantizable_op_details

    def _validate_and_inference_input_output(self, graph_def):
        """ As node name is part of tensor name and deterministic tensor name is needed
        for session inference during evaluation and calibration, lpot has an assumption
        that tensor name equal to node's first tensor if ':idx' not in tensor name.
        this means inputs/outputs from user or auto detected will be treated as tensor names 
        and add ':0' when ':idx' not in inputs/outputs.

        Args:
            graph_def (tf.compat.v1.GraphDef): model graph definition.
        """
        from .tf_utils.util import validate_graph_input, validate_graph_output
        from .tf_utils.util import get_input_node_names, get_output_node_names

        self.output_tensor_names = self.output_tensor_names \
            if validate_graph_output(graph_def, self.output_node_names) else \
            get_output_node_names(graph_def)

        self.input_tensor_names = self.input_tensor_names \
            if validate_graph_input(graph_def, self.input_node_names) else \
            get_input_node_names(graph_def)

        self.output_node_names = list(
            set([x.split(":")[0] for x in self.output_tensor_names]))
        self.input_node_names = list(
            set([x.split(":")[0] for x in self.input_tensor_names]))

        assert self.input_tensor_names
        assert self.output_tensor_names

    def query_fw_capability(self, model):
        """Collect the model-wise and op-wise configuration for quantization.

        Args:
            model (tf.compat.v1.GraphDef): model definition.

        Returns:
            [dict]: model-wise & op-wise configuration for quantization.
        """
        import tensorflow as tf
        from .tf_utils.graph_rewriter.generic.pre_optimize import PreOptimization
        from .tf_utils.graph_rewriter.graph_info import TFLowbitPrecisionPatterns
        from .tf_utils.util import get_graph_def

        graph_def = get_graph_def(model, self.output_node_names)
        self._validate_and_inference_input_output(graph_def)
        self.pre_optimizer_handle = PreOptimization(model,
                                                    self.input_tensor_names,
                                                    self.output_tensor_names)

        self.pre_optimized_graph = self.pre_optimizer_handle.get_optimized_graphdef()
        self.exclude_node_names = self.pre_optimizer_handle.get_excluded_node_names()
        tf_version = tf.version.VERSION
        patterns = TFLowbitPrecisionPatterns(tf_version).get_supported_patterns()
        matched_nodes = self.pre_optimizer_handle.get_matched_nodes(patterns)

        def check_match(patterns, input_pattern):
            for i in patterns:
                if input_pattern == [i for i in i.replace('+', ' ').strip().split(' ') if i]:
                    return True
            return False

        copied_matched_nodes = copy.deepcopy(matched_nodes)
        for i in copied_matched_nodes:
            if i[-1][0] in self.query_handler.get_op_types()['int8']:
                continue

            if not self.pre_optimizer_handle.has_positive_input(i[0]) and \
                not check_match(self.query_handler.get_fuse_patterns()['int8'], i[-1]):
                matched_nodes.remove(i)

        del copied_matched_nodes

        self._query_quantizable_ops(matched_nodes)
        capability = {
            'optypewise': self.get_optype_wise_ability(),
        }
        capability['opwise'] = self.quantizable_op_details
        logger.debug('Dump framework quantization capability:')
        logger.debug(capability)

        return capability

    def inspect_tensor(self, model, dataloader, op_list=[], iteration_list=[]):
        """Collect the specified tensor's output on specified iteration.

        Args:
            model (tf.compat.v1.GraphDef): model definition.
            dataloader (generator): generate the data and labels
            op_list (list, optional): the specified op names' list. Defaults to [].
            iteration_list (list, optional): the specified iteration. Defaults to [].

        Returns:
            [dict]: the key is op_name while the value is the ndarray tensor.
        """
        logger.info("Start to run inspect_tensor..")
        quantized_model = os.path.join(os.getcwd(), "tf_quantized.pb")
        from .tf_utils.graph_converter import GraphConverter

        converter = GraphConverter(self.pre_optimized_graph if self.pre_optimized_graph else model,
                                   quantized_model,
                                   inputs=self.input_tensor_names,
                                   outputs=self.output_tensor_names,
                                   qt_config=self.quantize_config,
                                   int8_sequences=self.op_wise_sequences,
                                   data_loader=dataloader)
        return converter.inspect_tensor(op_list, iteration_list, self.work_dir)

    def quantize_input(self, model):
        ''' quantize the model to be able to take quantized input
            remove graph QuantizedV2 op and move its input tensor to QuantizedConv2d
            and calculate the min-max scale

            Args:
                model (tf.compat.v1.GraphDef): The model to quantize input

            Return:
                model (tf.compat.v1.GraphDef): The quantized input model
                scale (float): The scale for dataloader to generate quantized input
        '''
        scale = None
        # quantize input only support tensorflow version > 2.1.0
        import tensorflow as tf
        if tf.version.VERSION < '2.1.0':
            logger.warning('quantize input need tensorflow version > 2.1.0')
            return model, scale

        graph_def = model.as_graph_def()
        node_name_mapping = {}
        quantize_nodes = []
        for node in graph_def.node:
            node_name_mapping[node.name] = node
            if node.op == 'QuantizeV2':
                quantize_nodes.append(node)

        target_quantize_nodes = []
        for node in quantize_nodes:
            # only support Quantizev2 input op Pad and Placeholder
            if (node_name_mapping[node.input[0]].op == 'Pad' and node_name_mapping[
                    node_name_mapping[node.input[0]].input[0]].op == 'Placeholder') or \
                    node_name_mapping[node.input[0]].op == 'Placeholder':
                target_quantize_nodes.append(node)
        assert len(target_quantize_nodes) == 1, 'only support 1 QuantizeV2 from Placeholder'
        quantize_node = target_quantize_nodes[0]

        quantize_node_input = node_name_mapping[quantize_node.input[0]]
        quantize_node_outputs = [node for node in graph_def.node
                                 if quantize_node.name in node.input]

        from .tf_utils.quantize_graph.quantize_graph_common import QuantizeGraphHelper
        if quantize_node_input.op == 'Pad':
            pad_node_input = node_name_mapping[quantize_node_input.input[0]]
            assert pad_node_input.op == 'Placeholder', \
                'only support Pad between QuantizeV2 and Placeholder'
            from tensorflow.python.framework import tensor_util
            paddings_tensor = tensor_util.MakeNdarray(node_name_mapping[
                quantize_node_input.input[1]].attr['value'].tensor).flatten()

            quantize_node.input[0] = quantize_node_input.input[0]
            for conv_node in quantize_node_outputs:
                assert 'Conv2D' in conv_node.op, 'only support QuantizeV2 to Conv2D'

                QuantizeGraphHelper.set_attr_int_list(conv_node,
                                                      "padding_list", paddings_tensor)
            graph_def.node.remove(quantize_node_input)

        from tensorflow.python.framework import dtypes
        QuantizeGraphHelper.set_attr_dtype(node_name_mapping[quantize_node.input[0]],
                                           "dtype", dtypes.qint8)

        for conv_node in quantize_node_outputs:
            for index, conv_input in enumerate(conv_node.input):
                if conv_input == quantize_node.name:
                    conv_node.input[index] = quantize_node.input[0]
                elif conv_input == quantize_node.name + ":1":
                    conv_node.input[index] = quantize_node.input[1]
                elif conv_input == quantize_node.name + ":2":
                    conv_node.input[index] = quantize_node.input[2]

        # get the input's min-max value and calculate scale
        max_node = node_name_mapping[quantize_node.input[2]]
        min_node = node_name_mapping[quantize_node.input[1]]
        max_value = max_node.attr['value'].tensor.float_val[0]
        min_value = min_node.attr['value'].tensor.float_val[0]
        scale = 127. / max(abs(max_value), abs(min_value))
        # remove QuantizeV2 node
        graph_def.node.remove(quantize_node)

        graph = tensorflow.Graph()
        with graph.as_default():
            # use name='' to avoid 'import/' to name scope
            tensorflow.import_graph_def(graph_def, name='')
        return graph, scale

    def get_optype_wise_ability(self):
        """Get the op type wise capability by generating the union value of each op type.
        Returns:
            [string dict]: the key is op type while the value is the
                           detail configurations of activation and weight for this op type.
        """
        res = OrderedDict()
        for op in self.quantizable_op_details.keys():
            if op[1] not in res.keys():
                res[op[1]] = {}
                res[op[1]]['activation'] = self.quantizable_op_details[op]['activation']
                if 'weight' in self.quantizable_op_details[op].keys():
                    res[op[1]]['weight'] = self.quantizable_op_details[op]['weight']
        return res

    def _pre_eval_hook(self, model):
        return model

    def _post_eval_hook(self, model):
        pass

    def save(self, model, path):
        pass


@singleton
class TensorflowQuery(QueryBackendCapability):

    def __init__(self, local_config_file=None):
        import tensorflow as tf

        super().__init__()
        self.version = tf.version.VERSION
        self.cfg = local_config_file
        self.cur_config = None
        self._one_shot_query()

    def _get_specified_version_cfg(self, data):
        """Get the configuration for the current runtime.
        If there's no matched configuration in the input yaml, we'll
        use the `default` field of yaml.

        Args:
            data (Yaml content): input yaml file.

        Returns:
            [dictionary]: the content for specific version.
        """
        default_config = None
        for sub_data in data:
            if sub_data['version']['name'] == self.version:
                return sub_data

            if sub_data['version']['name'] == 'default':
                default_config = sub_data

        return default_config

    def _one_shot_query(self):
        with open(self.cfg) as f:
            content = yaml.safe_load(f)
            try:
                self.cur_config = self._get_specified_version_cfg(content)
            except Exception as e:
                self.logger.info("Failed to parse {} due to {}".format(self.cfg, str(e)))
                self.cur_config = None
                raise ValueError("Please check the {} format.".format(self.cfg))

    def get_version(self):
        """Get the current backend version infomation.

        Returns:
            [string]: version string.
        """
        return self.cur_config['version']['name']

    def get_precisions(self):
        """Get supported precisions for current backend.

        Returns:
            [string list]: the precisions' name.
        """
        return self.cur_config['precisions']['names']

    def get_op_types(self):
        """Get the supported op types by all precisions.

        Returns:
            [dictionary list]: A list composed of dictionary which key is precision
            and value is the op types.
        """
        return self.cur_config['ops']

    def get_fuse_patterns(self):
        """Get supported patterns by low precisions.

        Returns:
            [dictionary list]: A list composed of dictionary which key is precision
            and value is the supported patterns.
        """
        return self.cur_config['patterns']

    def get_quantization_capability(self):
        """Get the supported op types' quantization capability.

        Returns:
            [dictionary list]: A list composed of dictionary which key is precision
            and value is a dict that describes all op types' quantization capability.
        """
        return self.cur_config['capabilities']

    def get_op_types_by_precision(self, precision):
        """Get op types per precision

        Args:
            precision (string): precision name

        Returns:
            [string list]: A list composed of op type.
        """
        assert precision in list(self.cur_config['ops'].keys())

        return self.cur_config['ops'][precision]

    def get_mixed_precision_combination(self):
        """Get the valid mixed precisions.

        Returns:
            [string list]: valid precision list.
        """
        if self.cur_config['precisions']['valid_mixed_precisions']:
            return [i.strip() for i in self.cur_config['precisions']['valid_mixed_precisions']]

        return [i.strip() for i in self.get_precisions().split(',')]

    def get_eightbit_patterns(self):
        """Get eightbit op wise sequences information.

        Returns:
            [dictionary]: key is the op type while value is the list of sequences start
                        with the op type same as key value.
        """
        quantizable_op_types = self.get_op_types_by_precision(
            'int8') + self.get_op_types_by_precision('uint8')
        int8_patterns = [i.replace(
            '+', ' ').split() for i in list(set(self.get_fuse_patterns()['int8'] +
                                                self.get_fuse_patterns()['uint8']))]

        res = {}
        for i in quantizable_op_types:
            res[i] = [[i]]

        for pattern in int8_patterns:
            op_type = pattern[0]
            if op_type in res:
                res[op_type].append(pattern)

        return res

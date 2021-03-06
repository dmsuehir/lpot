#
# -*- coding: utf-8 -*-
#
# Copyright (c) 2020 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
#

from __future__ import division
import time
import numpy as np
import tensorflow as tf
from lpot.adaptor.tf_utils.util import write_graph, get_graph_def
from argparse import ArgumentParser
tf.compat.v1.disable_eager_execution()

class eval_object_detection_optimized_graph(object):
    
    def __init__(self):
        arg_parser = ArgumentParser(description='Parse args')

        arg_parser.add_argument('-g',
                            "--input-graph",
                            help='Specify the input graph.',
                            dest='input_graph')
        arg_parser.add_argument('--config', type=str, default='')
        arg_parser.add_argument('--output_model', type=str, default='')
        arg_parser.add_argument('--tune', action='store_true', default=False)
        arg_parser.add_argument('--benchmark', dest='benchmark',
                            action='store_true', help='run benchmark')
        self.args = arg_parser.parse_args()
        
    def run(self):
        if self.args.tune:
            from lpot import Quantization
            quantizer = Quantization(self.args.config)
            q_model = quantizer(self.args.input_graph)
            try:
                write_graph(q_model.as_graph_def(), self.args.output_model)
            except Exception as e:
                print("Failed to save model due to {}".format(str(e)))
                
        if self.args.benchmark:
            from lpot import Benchmark
            evaluator = Benchmark(self.args.config)
            results = evaluator(model=self.args.input_graph)
            for mode, result in results.items():
                acc, batch_size, result_list = result
                latency = np.array(result_list).mean() / batch_size

                print('\n{} mode benchmark result:'.format(mode))
                print('Accuracy is {:.3f}'.format(acc))
                print('Batch size = {}'.format(batch_size))
                print('Latency: {:.3f} ms'.format(latency * 1000))
                print('Throughput: {:.3f} images/sec'.format(1./ latency))

if __name__ == "__main__":
    evaluate_opt_graph = eval_object_detection_optimized_graph()
    evaluate_opt_graph.run()

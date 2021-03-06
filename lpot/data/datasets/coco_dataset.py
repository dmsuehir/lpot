#!/usr/bin/env python
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
# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
from lpot.utils.utility import LazyImport
from .dataset import dataset_registry, IterableDataset, Dataset
tf = LazyImport('tensorflow')

@dataset_registry(dataset_type="COCORecord", framework="tensorflow", dataset_format='')
class COCORecordDataset(IterableDataset):
    """Configuration for Coco dataset."""

    def __new__(cls, root, num_cores=28, transform=None, filter=filter):
        record_iterator = tf.compat.v1.python_io.tf_record_iterator(root)
        example = tf.train.SequenceExample()
        for element in record_iterator:
            example.ParseFromString(element)
            break
        feature = example.context.feature
        if len(feature['image/object/class/text'].bytes_list.value) == 0 \
            and len(feature['image/object/class/label'].int64_list.value) == 0:
            raise ValueError("Tfrecord format is incorrect, please refer\
                'https://github.com/tensorflow/models/blob/master/research/\
                object_detection/dataset_tools/create_coco_tf_record.py' to\
                create correct tfrecord")

        from tensorflow.python.data.experimental import parallel_interleave
        tfrecord_paths = [root]
        ds = tf.data.TFRecordDataset.list_files(tfrecord_paths)
        ds = ds.apply(
            parallel_interleave(tf.data.TFRecordDataset,
                                cycle_length=num_cores,
                                block_length=5,
                                sloppy=True,
                                buffer_output_elements=10000,
                                prefetch_input_elements=10000))
        ds = ds.map(transform, num_parallel_calls=None)
        if filter is not None:
            ds = ds.filter(filter)
        ds = ds.prefetch(buffer_size=1000)
        ds.batch(1)
        return ds

    def __iter__(self):
        ds_iterator = tf.compat.v1.data.make_one_shot_iterator(self)
        iter_tensors = ds_iterator.get_next()
        from tensorflow.python.framework.errors_impl import OutOfRangeError
        data_config = tf.compat.v1.ConfigProto()
        data_config.use_per_session_threads = 1
        data_config.intra_op_parallelism_threads = 1
        data_config.inter_op_parallelism_threads = 16
        with tf.compat.v1.Session(config=data_config) as sess:
            while True:
                try:
                    outputs = sess.run(iter_tensors)
                    yield outputs[0]
                except OutOfRangeError:
                    return

@dataset_registry(dataset_type="COCORaw", framework="tensorflow", dataset_format='')
class COCORawDataset(Dataset):
    """Configuration for Coco raw dataset."""

    def __init__(self, root, img_dir='val2017', \
            anno_dir='annotations/instances_val2017.json', num_cores=28, \
                transform=None, filter=filter):
        import json
        import os
        import numpy as np
        from pycocotools.coco import COCO
        from lpot.metric.coco_label_map import category_map
        data_config = tf.compat.v1.ConfigProto()
        data_config.use_per_session_threads = 1
        data_config.intra_op_parallelism_threads = 1
        data_config.inter_op_parallelism_threads = 16
        self.sess = tf.compat.v1.Session(config=data_config)
        self.image_list = []
        self.transform = transform
        self.filter = filter
        img_path = os.path.join(root, img_dir)
        anno_path = os.path.join(root, anno_dir)
        coco = COCO(anno_path)
        img_ids = coco.getImgIds()
        cat_ids = coco.getCatIds()
        for idx, img_id in enumerate(img_ids):
            img_info = {}
            bboxes = []
            labels = []
            ids = []
            img_detail = coco.loadImgs(img_id)[0]
            ids.append(img_detail['file_name'].encode('utf-8'))
            pic_height = img_detail['height']
            pic_width = img_detail['width']

            ann_ids = coco.getAnnIds(imgIds=img_id,catIds=cat_ids)
            anns = coco.loadAnns(ann_ids)
            for ann in anns:
                bbox = ann['bbox']
                if len(bbox) == 0:
                    continue
                bbox = [bbox[0]/float(pic_width), bbox[1]/float(pic_height),\
                    bbox[2]/float(pic_width), bbox[3]/float(pic_height)]
                bboxes.append([bbox[1], bbox[0], bbox[1]+bbox[3], bbox[0]+bbox[2]])
                labels.append(category_map[ann['category_id']].encode('utf8'))
            img_file = os.path.join(img_path, img_detail['file_name'])
            if not os.path.exists(img_file) or len(bboxes) == 0:
                continue
            self.image_list.append(
                (img_file, [np.array(bboxes), np.array(labels), np.array([]),\
                 np.array(img_detail['file_name'].encode('utf-8'))]))
            
    def __len__(self):
        return len(self.image_list)

    def __getitem__(self, index):
        from PIL import Image
        sample = self.image_list[index]
        label = sample[1]
        with Image.open(sample[0]) as image:
            if self.transform is not None:
                image, label = self.transform((image, label))
                image = self.sess.run(image)
            return (image, label)


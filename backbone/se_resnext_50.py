import os
import sys
import glob
import random
import math
import datetime
import itertools
import json
import re
import logging
from collections import OrderedDict
import numpy as np
import scipy.misc
import tensorflow as tf
import keras
import keras.backend as K
import keras.layers as KL
import keras.initializers as KI
import keras.engine as KE
import keras.models as KM

class BatchNorm(KL.BatchNormalization):
    """Batch Normalization class. Subclasses the Keras BN class and
    hardcodes training=False so the BN layer doesn't update
    during training.

    Batch normalization has a negative effect on training if batches are small
    so we disable it here.
    """

    def call(self, inputs, training=None):
        return super(self.__class__, self).call(inputs, training=False)

def identity_block(input_tensor, kernel_size, filters, stage, block, cardinality=32):
    filters1, filters2, filters3 = filters
    grouped_filters = int(filters2 / cardinality)

    if K.image_data_format() == 'channels_last':
        bn_axis = 3
    else:
        bn_axis = 1

    group_list = []
        
    block_name = str(stage) + "_" + str(block)
    conv_name_base = "conv" + block_name
    relu_name_base = "relu" + block_name

    x = KL.Conv2D(filters1, (1, 1), use_bias=False, name=conv_name_base + '_x1')(input_tensor)
    x = BatchNorm(axis=bn_axis, name=conv_name_base + '_x1_bn')(x)
    x = KL.Activation('relu', name=relu_name_base + '_x1')(x)

    # x = KL.Conv2D(filters2, kernel_size, padding='same', use_bias=False, name=conv_name_base + '_x2')(x)
    # x = BatchNorm(axis=bn_axis, name=conv_name_base + '_x2_bn')(x)
    # x = KL.Activation('relu', name=relu_name_base + '_x2')(x)

    for c in range(cardinality):
        x = KL.Lambda(lambda z: z[:, :, :, c * grouped_filters:(c + 1) * grouped_filters])(input_tensor)
        x = KL.Conv2D(grouped_filters, kernel_size, padding='same', use_bias=False, name=conv_name_base + '_x2'+ "_" + str(c))(x)
        group_list.append(x)

    group_merge = KL.Concatenate(axis=3)(group_list)
    x = BatchNorm(axis=bn_axis, name=conv_name_base + '_x2_bn')(group_merge)
    x = KL.Activation('relu', name=relu_name_base + '_x2')(x)


    x = KL.Conv2D(filters3, (1, 1), use_bias=False, name=conv_name_base + '_x3')(x)
    x = BatchNorm(axis=bn_axis, name=conv_name_base + '_x3_bn')(x)

    se = KL.GlobalAveragePooling2D(name='pool' + block_name + '_gap')(x)
    se = KL.Dense(filters3 // 16, activation='relu', name = 'fc' + block_name + '_sqz')(se)
    se = KL.Dense(filters3, activation='sigmoid', name = 'fc' + block_name + '_exc')(se)
    se = KL.Reshape([1, 1, filters3])(se)
    x = KL.Multiply(name='scale' + block_name)([x, se])

    x = KL.Add(name='block_' + block_name)([x, input_tensor])
    x = KL.Activation('relu', name=relu_name_base)(x)
    return x

def conv_block(input_tensor, kernel_size, filters, stage, block, strides=(2, 2), cardinality=32):
    filters1, filters2, filters3 = filters
    grouped_filters = int(filters2 / cardinality)

    if K.image_data_format() == 'channels_last':
        bn_axis = 3
    else:
        bn_axis = 1

    group_list = []
        
    block_name = str(stage) + "_" + str(block)
    conv_name_base = "conv" + block_name
    relu_name_base = "relu" + block_name

    x = KL.Conv2D(filters1, (1, 1), use_bias=False, name=conv_name_base + '_x1')(input_tensor)
    x = BatchNorm(axis=bn_axis, name=conv_name_base + '_x1_bn')(x)
    x = KL.Activation('relu', name=relu_name_base + '_x1')(x)

    # x = KL.Conv2D(filters2, kernel_size, strides=strides, padding='same', use_bias=False, name=conv_name_base + '_x2')(x)
    # x = BatchNorm(axis=bn_axis, name=conv_name_base + '_x2_bn')(x)
    # x = KL.Activation('relu', name=relu_name_base + '_x2')(x)

    for c in range(cardinality):
        x = KL.Lambda(lambda z: z[:, :, :, c * grouped_filters:(c + 1) * grouped_filters])(input_tensor)
        x = KL.Conv2D(grouped_filters, kernel_size, strides=strides, padding='same', use_bias=False, name=conv_name_base + '_x2'+ "_" + str(c))(x)
        group_list.append(x)

    group_merge = KL.Concatenate(axis=3)(group_list)
    x = BatchNorm(axis=bn_axis, name=conv_name_base + '_x2_bn')(group_merge)
    x = KL.Activation('relu', name=relu_name_base + '_x2')(x)
    
    x = KL.Conv2D(filters3, (1, 1), use_bias=False, name=conv_name_base + '_x3')(x)
    x = BatchNorm(axis=bn_axis, name=conv_name_base + '_x3_bn')(x)
    
    se = KL.GlobalAveragePooling2D(name='pool' + block_name + '_gap')(x)
    se = KL.Dense(filters3 // 16, activation='relu', name = 'fc' + block_name + '_sqz')(se)
    se = KL.Dense(filters3, activation='sigmoid', name = 'fc' + block_name + '_exc')(se)
    se = KL.Reshape([1, 1, filters3])(se)
    x = KL.Multiply(name='scale' + block_name)([x, se])
    
    shortcut = KL.Conv2D(filters3, (1, 1), strides=strides, use_bias=False, name=conv_name_base + '_prj')(input_tensor)
    shortcut = BatchNorm(axis=bn_axis, name=conv_name_base + '_prj_bn')(shortcut)

    x = KL.Add(name='block_' + block_name)([x, shortcut])
    x = KL.Activation('relu', name=relu_name_base)(x)
    return x

def resnet_graph(input_image, stage5=False):

    """
    Model generator
    :param input_image: model input
    :param stage5: enables or disables the last stage
    :return: returns the network stages
    """

    # Stage 1
    x = KL.ZeroPadding2D((3, 3))(input_image)
    x = KL.Conv2D(64, (7, 7), strides=(2, 2), use_bias=False, name='conv1')(x)
    x = BatchNorm(axis=3, name='conv1_bn')(x)
    x = KL.Activation('relu', name='relu1')(x)
    C1 = x = KL.MaxPooling2D((3, 3), strides=(2, 2), padding="same", name='pool1')(x)
    
    # Stage 2
    x = conv_block(x, 3, [64, 64, 256], stage=2, block=1, strides=(1, 1))
    x = identity_block(x, 3, [64, 64, 256], stage=2, block=2)
    C2 = x = identity_block(x, 3, [64, 64, 256], stage=2, block=3)

    # Stage 3
    x = conv_block(x, 3, [128, 128, 512], stage=3, block=1)
    x = identity_block(x, 3, [128, 128, 512], stage=3, block=2)
    x = identity_block(x, 3, [128, 128, 512], stage=3, block=3)
    C3 = x = identity_block(x, 3, [128, 128, 512], stage=3, block=4)

    # Stage 4
    x = conv_block(x, 3, [256, 256, 1024], stage=4, block=1)
    x = identity_block(x, 3, [256, 256, 1024], stage=4, block=2)
    x = identity_block(x, 3, [256, 256, 1024], stage=4, block=3)
    x = identity_block(x, 3, [256, 256, 1024], stage=4, block=4)
    x = identity_block(x, 3, [256, 256, 1024], stage=4, block=5)
    x = identity_block(x, 3, [256, 256, 1024], stage=4, block=6)
    C4 = x

    # Stage 5
    if stage5:
        x = conv_block(x, 3, [512, 512, 2048], stage=5, block=1)
        x = identity_block(x, 3, [512, 512, 2048], stage=5, block=2)
        C5 = x = identity_block(x, 3, [512, 512, 2048], stage=5, block=3)
    else:
        C5 = None

    return [C1, C2, C3, C4, C5]

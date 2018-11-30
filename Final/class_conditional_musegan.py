# -*- coding: utf-8 -*-
"""Class Conditional MuseGAN.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1FCEiimTcDCTFjMTwAHTKMW69MxmkKeAq
"""

import numpy as np
import os, sys
from tqdm import trange
from os.path import dirname, abspath, basename, exists, splitext, join

import tensorflow as tf
import tensorflow.contrib.slim as slim
from tensorflow.python.framework import ops

from CONFIG import *

#Operations for implementing binary neurons. Code is from the R2RT blog post:
#https://r2rt.com/binary-stochastic-neurons-in-tensorflow.html


def binary_round(x):
    """
    Rounds a tensor whose values are in [0,1] to a tensor with values in
    {0, 1}, using the straight through estimator for the gradient.
    """
    g = tf.get_default_graph()

    with ops.name_scope("BinaryRound") as name:
        with g.gradient_override_map({"Round": "Identity"}):
            return tf.round(x, name=name)

def bernoulli_sample(x):
    """
    Uses a tensor whose values are in [0,1] to sample a tensor with values
    in {0, 1}, using the straight through estimator for the gradient.
    E.g., if x is 0.6, bernoulliSample(x) will be 1 with probability 0.6,
    and 0 otherwise, and the gradient will be pass-through (identity).
    """
    g = tf.get_default_graph()

    with ops.name_scope("BernoulliSample") as name:
        with g.gradient_override_map({"Ceil": "Identity",
                                      "Sub": "BernoulliSample_ST"}):
            return tf.ceil(x - tf.random_uniform(tf.shape(x)), name=name)

#@ops.RegisterGradient("BernoulliSample_ST")
def bernoulli_sample_ST(op, grad):
    return [grad, tf.zeros(tf.shape(op.inputs[1]))]

def pass_through_sigmoid(x, slope=1):
    """Sigmoid that uses identity function as its gradient"""
    g = tf.get_default_graph()
    with ops.name_scope("PassThroughSigmoid") as name:
        with g.gradient_override_map({"Sigmoid": "Identity"}):
            return tf.sigmoid(x, name=name)

def binary_stochastic_ST(x, slope_tensor=None, pass_through=True,
                        stochastic=True):
    """
    Sigmoid followed by either a random sample from a bernoulli distribution
    according to the result (binary stochastic neuron) (default), or a
    sigmoid followed by a binary step function (if stochastic == False).
    Uses the straight through estimator. See
    https://arxiv.org/abs/1308.3432.
    Arguments:
    * x: the pre-activation / logit tensor
    * slope_tensor: if passThrough==False, slope adjusts the slope of the
        sigmoid function for purposes of the Slope Annealing Trick (see
        http://arxiv.org/abs/1609.01704)
    * pass_through: if True (default), gradient of the entire function is 1
        or 0; if False, gradient of 1 is scaled by the gradient of the
        sigmoid (required if Slope Annealing Trick is used)
    * stochastic: binary stochastic neuron if True (default), or step
        function if False
    """
    if slope_tensor is None:
        slope_tensor = tf.constant(1.0)

    if pass_through:
        p = pass_through_sigmoid(x)
    else:
        p = tf.sigmoid(slope_tensor * x)

    if stochastic:
        return bernoulli_sample(p), p
    else:
        return binary_round(p), p

def binary_stochastic_REINFORCE(x, loss_op_name="loss_by_example"):
    """
    Sigmoid followed by a random sample from a bernoulli distribution
    according to the result (binary stochastic neuron). Uses the REINFORCE
    estimator. See https://arxiv.org/abs/1308.3432.
    NOTE: Requires a loss operation with name matching the argument for
    loss_op_name in the graph. This loss operation should be broken out by
    example (i.e., not a single number for the entire batch).
    """
    g = tf.get_default_graph()

    with ops.name_scope("BinaryStochasticREINFORCE"):
        with g.gradient_override_map({"Sigmoid": "BinaryStochastic_REINFORCE",
                                      "Ceil": "Identity"}):
            p = tf.sigmoid(x)

            reinforce_collection = g.get_collection("REINFORCE")
            if not reinforce_collection:
                g.add_to_collection("REINFORCE", {})
                reinforce_collection = g.get_collection("REINFORCE")
            reinforce_collection[0][p.op.name] = loss_op_name

            return tf.ceil(p - tf.random_uniform(tf.shape(x)))


@ops.RegisterGradient("BinaryStochastic_REINFORCE")
def _binaryStochastic_REINFORCE(op, _):
    """Unbiased estimator for binary stochastic function based on REINFORCE."""
    loss_op_name = op.graph.get_collection("REINFORCE")[0][op.name]
    loss_tensor = op.graph.get_operation_by_name(loss_op_name).outputs[0]

    sub_tensor = op.outputs[0].consumers()[0].outputs[0] #subtraction tensor
    ceil_tensor = sub_tensor.consumers()[0].outputs[0] #ceiling tensor

    outcome_diff = (ceil_tensor - op.outputs[0])

    # Provides an early out if we want to avoid variance adjustment for
    # whatever reason (e.g., to show that variance adjustment helps)
    if op.graph.get_collection("REINFORCE")[0].get("no_variance_adj"):
        return outcome_diff * tf.expand_dims(loss_tensor, 1)

    outcome_diff_sq = tf.square(outcome_diff)
    outcome_diff_sq_r = tf.reduce_mean(outcome_diff_sq, reduction_indices=0)
    outcome_diff_sq_loss_r = tf.reduce_mean(
        outcome_diff_sq * tf.expand_dims(loss_tensor, 1), reduction_indices=0)

    l_bar_num = tf.Variable(tf.zeros(outcome_diff_sq_r.get_shape()),
                            trainable=False)
    l_bar_den = tf.Variable(tf.ones(outcome_diff_sq_r.get_shape()),
                            trainable=False)

    # Note: we already get a decent estimate of the average from the minibatch
    decay = 0.95
    train_l_bar_num = tf.assign(l_bar_num, l_bar_num*decay +\
                                            outcome_diff_sq_loss_r*(1-decay))
    train_l_bar_den = tf.assign(l_bar_den, l_bar_den*decay +\
                                            outcome_diff_sq_r*(1-decay))


    with tf.control_dependencies([train_l_bar_num, train_l_bar_den]):
        l_bar = train_l_bar_num/(train_l_bar_den + 1e-4)
        l = tf.tile(tf.expand_dims(loss_tensor, 1),
                    tf.constant([1, l_bar.get_shape().as_list()[0]]))
        return outcome_diff * (l - l_bar)

def binary_wrapper(pre_activations_tensor, estimator,
                   stochastic_tensor=tf.constant(True), pass_through=True,
                   slope_tensor=tf.constant(1.0)):
    """
    Turns a layer of pre-activations (logits) into a layer of binary
    stochastic neurons
    Keyword arguments:
    *estimator: either ST or REINFORCE
    *stochastic_tensor: a boolean tensor indicating whether to sample from a
        bernoulli distribution (True, default) or use a step_function (e.g.,
        for inference)
    *pass_through: for ST only - boolean as to whether to substitute
        identity derivative on the backprop (True, default), or whether to
        use the derivative of the sigmoid
    *slope_tensor: for ST only - tensor specifying the slope for purposes of
        slope annealing trick
    """
    if estimator == 'straight_through':
        if pass_through:
            return tf.cond(
                stochastic_tensor,
                lambda: binary_stochastic_ST(pre_activations_tensor),
                lambda: binary_stochastic_ST(pre_activations_tensor,
                                             stochastic=False))
        else:
            return tf.cond(
                stochastic_tensor,
                lambda: binary_stochastic_ST(pre_activations_tensor,
                                             slope_tensor, False),
                lambda: binary_stochastic_ST(pre_activations_tensor,
                                             slope_tensor, False, False))

    elif estimator == 'reinforce':
        # binaryStochastic_REINFORCE was designed to only be stochastic, so
        # using the ST version for the step fn for purposes of using step
        # fn at evaluation / not for training
        return tf.cond(
            stochastic_tensor,
            lambda: binary_stochastic_REINFORCE(pre_activations_tensor),
            lambda: binary_stochastic_ST(pre_activations_tensor,
                                         stochastic=False))

    else:
        raise ValueError("Unrecognized estimator.")

#ARCHITECTURE FUNCTIONS

def Generator(input_genre, latent_vector, LATENT_SIZE, NUM_TRACKS, NUM_CLASSES):

  #Class Embedding Layer
  embedding_layer =  tf.keras.layers.Embedding(NUM_CLASSES, LATENT_SIZE, embeddings_initializer='glorot_normal', name = 'generator_embedding')
  embedding_lookup = embedding_layer.__call__(input_genre)
  class_input = tf.multiply(latent_vector, embedding_lookup, name = 'generator_multiply')
  #print(class_input)


  #Shared Generator
  def shared_generator(class_input):
    dense_1 = tf.layers.dense(class_input, (3*512), activation = 'relu', name='generator_dense_1')
    bn_1 = tf.layers.batch_normalization(dense_1, name='generator_batch_norm_1')
    reshape_1 = tf.reshape(bn_1, [-1,3,1,1,512], name = 'generator_reshape_1')
    trans_conv3d_1 = tf.layers.conv3d_transpose(reshape_1, 256, (2, 1, 1), (1, 1, 1), activation = 'relu', name='generator_transconv3d_1')
    bn_2 = tf.layers.batch_normalization(trans_conv3d_1, name='generator_batch_norm_2')
    trans_conv3d_2 =  tf.layers.conv3d_transpose(bn_2, 128, (1, 4, 1), (1, 4, 1), activation = 'relu', name='generator_transconv3d_2')
    bn_3 = tf.layers.batch_normalization(trans_conv3d_2, name='generator_batch_norm_3')
    trans_conv3d_3 =  tf.layers.conv3d_transpose(bn_3, 128, (1, 1, 3), (1, 1, 3), activation = 'relu', name='generator_transconv3d_3')
    bn_4 = tf.layers.batch_normalization(trans_conv3d_3, name='generator_batch_norm_4')
    trans_conv3d_4 =  tf.layers.conv3d_transpose(bn_4, 64, (1, 4, 1), (1, 4, 1), activation = 'relu', name='generator_transconv3d_4')
    bn_5 = tf.layers.batch_normalization(trans_conv3d_4, name='generator_batch_norm_5')
    trans_conv3d_5 =  tf.layers.conv3d_transpose(bn_5, 64, (1, 1, 3), (1, 1, 2), activation = 'relu', name='generator_transconv3d_5')
    shared_out = tf.layers.batch_normalization(trans_conv3d_5, name='generator_batch_norm_6')
    #print(shared_out)
    return shared_out

  #Private Generator
  def pitch_time_private(shared_out, track_num):
    pt_conv3d_1 = tf.layers.conv3d_transpose(shared_out, 64, (1, 1, 12), (1, 1, 12), activation = 'relu', name = ('generator_pt_conv3d_1'+str(track_num)))
    pt_bn_1 = tf.layers.batch_normalization(pt_conv3d_1, name=('generator_pt_bn_1'+str(track_num)))
    pt_conv3d_2 = tf.layers.conv3d_transpose(pt_bn_1, 32, (1, 6, 1), (1, 6, 1), activation = 'relu', name = ('generator_pt_conv3d_2'+str(track_num)))
    pt_bn_2 = tf.layers.batch_normalization(pt_conv3d_2, name=('generator_pt_bn_2'+str(track_num)))
    return pt_bn_2

  def time_pitch_private(shared_out, track_num):
    tp_conv3d_1 = tf.layers.conv3d_transpose(shared_out, 64, (1, 6, 1), (1, 6, 1), activation = 'relu', name = ('generator_tp_conv3d_1'+str(track_num)))
    tp_bn_1 = tf.layers.batch_normalization(tp_conv3d_1, name=('generator_tp_bn_1'+str(track_num)))
    tp_conv3d_2 = tf.layers.conv3d_transpose(tp_bn_1, 32, (1, 1, 12), (1, 1, 12), activation = 'relu', name = ('generator_tp_conv3d_2'+str(track_num)))
    tp_bn_2 = tf.layers.batch_normalization(tp_conv3d_2, name= ('generator_tp_bn_2'+str(track_num)))
    return tp_bn_2

  def merged_private(private_out, track_num):
    merged_conv3d = tf.layers.conv3d_transpose(private_out, 1, (1, 1, 1), (1, 1, 1), activation = 'sigmoid', name = ('generator_merged_conv3d'+str(track_num)))
    merged_bn = tf.layers.batch_normalization(merged_conv3d, name=('generator_merged_bn'+str(track_num)))
    return merged_bn

  #Loop Private Generators over all tracks and concat
  shared_out = shared_generator(class_input)
  #print(shared_out)
  private_out = []
  for i in range(NUM_TRACKS):
    private_out.append (merged_private(tf.concat([pitch_time_private(shared_out, i), time_pitch_private(shared_out, i)], -1), i))


  #print(private_out)
  generator_out = tf.concat(private_out,-1)
  return generator_out


def Discriminator(refiner_out, NUM_TRACKS):
  def my_leaky_relu(x):
    return tf.nn.leaky_relu(x, alpha=.5)

  #Private Discriminiator
  def pitch_time_private(refiner_out, track_num):
    pt_conv3d_1 = tf.layers.conv3d(refiner_out, 32, (1, 1, 12), (1, 1, 12), activation = my_leaky_relu, name = ('discriminator_pt_conv3d_1'+str(track_num)))
    #pt_bn_1 = tf.layers.batch_normalization(pt_conv3d_1, name=('d_pt_bn_1'+str(track_num)))
    pt_conv3d_2 = tf.layers.conv3d(pt_conv3d_1, 64, (1, 6, 1), (1, 6, 1), activation = my_leaky_relu, name = ('discriminator_pt_conv3d_2'+str(track_num)))
    #pt_bn_2 = tf.layers.batch_normalization(pt_conv3d_2, name=('d_pt_bn_2'+str(track_num)))
    return pt_conv3d_2

  def time_pitch_private(refiner_out, track_num):
    tp_conv3d_1 = tf.layers.conv3d(refiner_out, 32, (1, 6, 1), (1, 6, 1), activation = my_leaky_relu, name = ('discriminator_tp_conv3d_1'+str(track_num)))
    #tp_bn_1 = tf.layers.batch_normalization(tp_conv3d_1, name=('d_tp_bn_1'+str(track_num)))
    tp_conv3d_2 = tf.layers.conv3d(tp_conv3d_1, 64, (1, 1, 12), (1, 1, 12), activation = my_leaky_relu, name = ('discriminator_tp_conv3d_2'+str(track_num)))
    #tp_bn_2 = tf.layers.batch_normalization(tp_conv3d_2, name= ('d_tp_bn_2'+str(track_num)))
    return tp_conv3d_2

  def merged_private(private_out, track_num):
    merged_conv3d = tf.layers.conv3d(private_out, 64, (1, 1, 1), (1, 1, 1), activation = my_leaky_relu, name = ('discriminator_merged_conv3d'+str(track_num)))
    #merged_bn = tf.layers.batch_normalization(merged_conv3d, name=('d_merged_bn'+str(track_num)))
    return merged_conv3d

  #Shared Discriminiator
  def shared_discriminator(private_discriminator_out):
    conv3d_1 = tf.layers.conv3d(private_discriminator_out, 128, (1, 4, 3), (1, 4, 2), activation = my_leaky_relu, name = ('discriminator_conv3d_1'))
    conv3d_2 = tf.layers.conv3d(conv3d_1, 256, (1, 4, 3), (1, 4, 2), activation = my_leaky_relu, name = ('discriminator_conv3d_2'))
    return conv3d_2

  #Chroma: reduce along octaves and tracks to determine chroma and then pass that into chroma discriminator
  def Chroma(refiner_out):
    reshape = tf.transpose(tf.reshape(refiner_out, [-1,5, 4, 4, 24, 7, 12]), [0,2,3,4,5,6,1], name = 'discriminator_chroma_reshape')
    sum_1 = tf.reduce_sum(reshape, axis=(3, 4), name = 'discriminator_chroma_sum')
    chroma_conv3d_1 = tf.layers.conv3d(sum_1, 64, (1, 1, 12), (1, 1, 12), activation = my_leaky_relu, name = ('discriminator_chroma_conv3d_1'))
    chroma_conv3d_2 = tf.layers.conv3d(chroma_conv3d_1, 128, (1, 4, 1), (1, 4, 1), activation = my_leaky_relu, name = ('discriminator_chroma_conv3d_2'))
    return chroma_conv3d_2

  def Onset(refiner_out):
    reshape = tf.transpose(tf.reshape(refiner_out, [-1,5, 4, 96, 84]), [0,2,3,4,1])
    padded = tf.pad(reshape[:, :, :-1, :, 1:], [[0, 0], [0, 0], [1, 0], [0, 0], [0, 0]])
    onset = tf.concat([tf.expand_dims(reshape[..., 0], -1), reshape[..., 1:] - padded], -1)
    sum_1 = tf.reduce_sum(onset, 3, keepdims = True, name='discriminator_onset_sum')
    onset_conv3d_1 = tf.layers.conv3d(sum_1, 32, (1, 6, 1), (1, 6, 1), activation = my_leaky_relu, name = ('discriminator_onset_conv3d_1'))
    onset_conv3d_2 = tf.layers.conv3d(onset_conv3d_1, 64, (1, 4, 1), (1, 4, 1), activation = my_leaky_relu, name = ('discriminator_onset_conv3d_2'))
    onset_conv3d_3 = tf.layers.conv3d(onset_conv3d_2, 128, (1, 4, 1), (1, 4, 1), activation = my_leaky_relu, name = ('discriminator_onset_conv3d_3'))
    return onset_conv3d_3

  def Merged_Discriminator(concated):
    merged_conv3d_1 = tf.layers.conv3d(concated, 512, (2, 1, 1), (1, 1, 1), activation = my_leaky_relu, name = ('discriminator_merged_conv3d_1'))
    #print(merged_conv3d_1)
    reshape = tf.reshape(merged_conv3d_1, [-1,512*3])
    #print(reshape)
    dense = tf.layers.dense(reshape, 1, name='discriminiator_dense_out')
    #print(dense)
    return dense

  private_out = []
  for i in range(NUM_TRACKS):
    track = tf.expand_dims(refiner_out[:,:,:,:, i],axis=-1)
    private_out.append (merged_private(tf.concat([pitch_time_private(track, i), time_pitch_private(track, i)], -1), i))

  private_discriminator_out = tf.concat(private_out,-1)
  #print(private_discriminator_out)
  private_discriminator_out = tf.reshape(private_discriminator_out, [-1, 4, 16, 7, 64])
  #print(private_discriminator_out)
  shared_discriminator_out = tf.reshape(shared_discriminator(private_discriminator_out), [-1, 4, 1, 1, 5*256])
  #print(shared_discriminator_out)

  chroma_out = Chroma(refiner_out)
  onset_out = Onset(refiner_out)
  concated = tf.concat([shared_discriminator_out, chroma_out, onset_out], -1)
  #print(concated)
  discrminiator_out = Merged_Discriminator(concated)
  return discrminiator_out


def Classifier(refiner_out, NUM_TRACKS, NUM_CLASSES):
  def my_leaky_relu(x):
    return tf.nn.leaky_relu(x, alpha=.5)

  #Private Discriminiator
  def pitch_time_private(refiner_out, track_num):
    pt_conv3d_1 = tf.layers.conv3d(refiner_out, 32, (1, 1, 12), (1, 1, 12), activation = my_leaky_relu, name = ('classifier_pt_conv3d_1'+str(track_num)))
    #pt_bn_1 = tf.layers.batch_normalization(pt_conv3d_1, name=('d_pt_bn_1'+str(track_num)))
    pt_conv3d_2 = tf.layers.conv3d(pt_conv3d_1, 64, (1, 6, 1), (1, 6, 1), activation = my_leaky_relu, name = ('classifier_pt_conv3d_2'+str(track_num)))
    #pt_bn_2 = tf.layers.batch_normalization(pt_conv3d_2, name=('d_pt_bn_2'+str(track_num)))
    return pt_conv3d_2

  def time_pitch_private(refiner_out, track_num):
    tp_conv3d_1 = tf.layers.conv3d(refiner_out, 32, (1, 6, 1), (1, 6, 1), activation = my_leaky_relu, name = ('classifier_tp_conv3d_1'+str(track_num)))
    #tp_bn_1 = tf.layers.batch_normalization(tp_conv3d_1, name=('d_tp_bn_1'+str(track_num)))
    tp_conv3d_2 = tf.layers.conv3d(tp_conv3d_1, 64, (1, 1, 12), (1, 1, 12), activation = my_leaky_relu, name = ('classifier_tp_conv3d_2'+str(track_num)))
    #tp_bn_2 = tf.layers.batch_normalization(tp_conv3d_2, name= ('d_tp_bn_2'+str(track_num)))
    return tp_conv3d_2

  def merged_private(private_out, track_num):
    merged_conv3d = tf.layers.conv3d(private_out, 64, (1, 1, 1), (1, 1, 1), activation = my_leaky_relu, name = ('classifier_merged_conv3d'+str(track_num)))
    #merged_bn = tf.layers.batch_normalization(merged_conv3d, name=('d_merged_bn'+str(track_num)))
    return merged_conv3d

  #Shared Discriminiator
  def shared_discriminator(private_discriminator_out):
    conv3d_1 = tf.layers.conv3d(private_discriminator_out, 128, (1, 4, 3), (1, 4, 2), activation = my_leaky_relu, name = ('classifier_conv3d_1'))
    conv3d_2 = tf.layers.conv3d(conv3d_1, 256, (1, 4, 3), (1, 4, 2), activation = my_leaky_relu, name = ('classifier_conv3d_2'))
    return conv3d_2

  #Chroma: reduce along octaves and tracks to determine chroma and then pass that into chroma discriminator
  def Chroma(refiner_out):
    reshape = tf.transpose(tf.reshape(refiner_out, [-1,5, 4, 4, 24, 7, 12]), [0,2,3,4,5,6,1], name = 'classifier_chroma_reshape')
    sum_1 = tf.reduce_sum(reshape, axis=(3, 4), name = 'classifier_chroma_sum')
    chroma_conv3d_1 = tf.layers.conv3d(sum_1, 64, (1, 1, 12), (1, 1, 12), activation = my_leaky_relu, name = ('classifier_chroma_conv3d_1'))
    chroma_conv3d_2 = tf.layers.conv3d(chroma_conv3d_1, 128, (1, 4, 1), (1, 4, 1), activation = my_leaky_relu, name = ('classifier_chroma_conv3d_2'))
    return chroma_conv3d_2

  def Onset(refiner_out):
    reshape = tf.transpose(tf.reshape(refiner_out, [-1,5, 4, 96, 84]), [0,2,3,4,1])
    padded = tf.pad(reshape[:, :, :-1, :, 1:], [[0, 0], [0, 0], [1, 0], [0, 0], [0, 0]])
    onset = tf.concat([tf.expand_dims(reshape[..., 0], -1), reshape[..., 1:] - padded], -1)
    sum_1 = tf.reduce_sum(onset, 3, keepdims = True, name='classifier_onset_sum')
    onset_conv3d_1 = tf.layers.conv3d(sum_1, 32, (1, 6, 1), (1, 6, 1), activation = my_leaky_relu, name = ('classifier_onset_conv3d_1'))
    onset_conv3d_2 = tf.layers.conv3d(onset_conv3d_1, 64, (1, 4, 1), (1, 4, 1), activation = my_leaky_relu, name = ('classifier_onset_conv3d_2'))
    onset_conv3d_3 = tf.layers.conv3d(onset_conv3d_2, 128, (1, 4, 1), (1, 4, 1), activation = my_leaky_relu, name = ('classifier_onset_conv3d_3'))
    return onset_conv3d_3

  def Merged_Classifier(concated, NUM_CLASSES):
    merged_conv3d_1 = tf.layers.conv3d(concated, 512, (2, 1, 1), (1, 1, 1), activation = my_leaky_relu, name = ('classifier_merged_conv3d_1'))
    reshape = tf.reshape(merged_conv3d_1, [-1, 512*3])
    dense = tf.layers.dense(reshape, NUM_CLASSES, activation = 'softmax', name='classifier_dense_out')
    return dense

  private_out = []
  for i in range(NUM_TRACKS):
    track = tf.expand_dims(refiner_out[:,:,:,:, i],axis=-1)
    private_out.append (merged_private(tf.concat([pitch_time_private(track, i), time_pitch_private(track, i)], -1), i))

  private_discriminator_out = tf.concat(private_out,-1)
  private_discriminator_out = tf.reshape(private_discriminator_out, [-1, 4, 16, 7, 64])
  shared_discriminator_out = tf.reshape(shared_discriminator(private_discriminator_out), [-1, 4, 1, 1, 5*256])
  chroma_out = Chroma(refiner_out)
  onset_out = Onset(refiner_out)
  concated = tf.concat([shared_discriminator_out, chroma_out, onset_out], -1)
  print(concated)
  classifier_out = Merged_Classifier(concated, NUM_CLASSES)
  return classifier_out


def Refiner(generator_out, NUM_TRACKS, RESIDUAL_LAYERS, SLOPE_TENSOR):

  def Residual_Unit(residual_input, residual_layer, track_num):
    #print(residual_input)
    activation = tf.layers.batch_normalization(tf.nn.relu(residual_input), name = ('refiner_activation' +str(residual_layer) + '_' + str(track_num)))
    #print(activation)
    conv3d_1 = tf.layers.conv3d(activation, 64, (1, 3, 12), (1, 1, 1), padding = 'same', activation = 'relu', name = ('refiner_conv3d_1'+str(residual_layer) + '_' + str(track_num)))
    batch_norm_1 = tf.layers.batch_normalization(conv3d_1, name = ('refiner_batch_normalization' +str(residual_layer) + '_' + str(track_num)))
    #print(batch_norm_1)
    conv3d_2 = tf.layers.conv3d(batch_norm_1, 1, (1, 3, 12), (1, 1, 1), padding = 'same', name = ('refiner_conv3d_2'+str(residual_layer) + '_' + str(track_num)))
    #print(conv3d_2)
    add = tf.add(residual_input, conv3d_2, name = ('add'+str(residual_layer) + '_' + str(track_num)))
    return add

  track = []
  residual_out = []
  BSN_out = []

  #break up output generator into list of tracks
  for ii in range(NUM_TRACKS):
    track.append(tf.expand_dims(generator_out[:,:,:,:,ii],axis=-1))

  #refiner network
  for ii in range(RESIDUAL_LAYERS):
    for jj in range(NUM_TRACKS):
      if ii == 0:
        residual_out.append(Residual_Unit(track[ii], jj, ii))
      elif ii == RESIDUAL_LAYERS-1: #if last layer apply stochastic binary neuron
        residual_out[jj] = Residual_Unit(residual_out[jj], jj, ii)
        BSN_out.append(binary_stochastic_ST(residual_out[jj], SLOPE_TENSOR, False, False)[0])
      else:
        residual_out[jj] = Residual_Unit(residual_out[jj], jj, ii)


  #return concatenation of tracks
  #print(residual_out)
  refiner_out = tf.concat(BSN_out,-1)
  return refiner_out

class Loss_Functions():
  def __init__(self, gp_coefficient, discriminator_coefficient):
    self.discriminator_coefficient = discriminator_coefficient
    self.classifier_coefficient = 1-discriminator_coefficient_coefficient
    self.gradient_penalty = gradient_penalty

def classifier_loss(data, labels, label_smoothing): # if label smoothing nonzero then is used
  return tf.losses.softmax_cross_entropy(labels, data, label_smoothing= label_smoothing)


def adverserial_loss(D_fake_data, D_real_data, real_input_data, G_out, gradient_penalty): #WGAN with gradient penalty
  discriminator_loss = (tf.reduce_mean(D_fake_data) - tf.reduce_mean(D_real_data))
  generator_loss = -tf.reduce_mean(D_fake_data)
  eps = tf.random_uniform([tf.shape(G_out)[0], 1, 1, 1, 1], 0.0, 1.0)
  inter = eps * real_input_data + (1. - eps) * G_out
  D_inter = Discriminator(inter, tf.shape(G_out)[4], tf.shape(G_out)[0])
  gradient = tf.gradients(D_inter, inter)[0]
  slopes = tf.sqrt(1e-8 + tf.reduce_sum(tf.square(gradient), tf.range(1, len(gradient.get_shape()))))
  gradient_penalty = tf.reduce_mean(tf.square(slopes - 1.0))
  discriminator_loss += (gradient_penalty* gradient_penalty)
  return discriminator_loss, generator_loss

class Data(object):
  def __init__(self, data_directory):
    #Only restrieves songs in folder that are from genres of interest
    songs_list = [element for element in os.listdir(data_directory) if element.split("-")[0] in GENRE_LIST]

    self.path = data_directory
    self.songs = songs_list
    self.index_in_epoch = 0
    self.num_examples = len(songs_list)

    #shuffle songs so genres are well represented in first epoch
    np.random.shuffle(self.songs)

  def get_batch(self):
    start = self.index_in_epoch
    self.index_in_epoch += BATCH_SIZE

    # When all the training data is ran, shuffles it
    if self.index_in_epoch > self.num_examples:
        np.random.shuffle(self.songs)

        # Start next epoch
        start = 0
        self.index_in_epoch = batch_size
        assert batch_size <= self.num_examples
    end = self.index_in_epoch

    #This unzipping is done to save on storage and memeory since the files are mostly 0's
    npz_data = [np.load(join(self.path,element))["data"] for element in self.songs[start:end]] #uncompress all npz files and load in "data" array
    batch_data = [element[0] for element in npz_data] #corresponds to the songs data which is stored in the first element of the data array
    batch_label = [GENRE_LIST.index(element[1]) for element in npz_data]  #corresponds to the label fo the song stored inteh first element of the data array

    return batch_data, batch_label

#BULD FULL MODEL FOR TESTING SHAPES
def main():
    tf.reset_default_graph()

    print("\n\n")
    print("Defining placeholders...")
    input_genre = tf.placeholder(dtype = tf.int32, shape = 1)
    latent_vector = tf.placeholder(dtype = tf.float32, shape = [None,LATENT_SIZE])
    real_data = tf.placeholder(dtype = tf.bool, shape = [None, NUM_BARS, BEATS_PER_BAR, NUM_NOTES, NUM_TRACKS])
    real_data = tf.cast(real_data, dtype= tf.float32)

    print("Constructing Model...")
    generator_out = Generator(input_genre, latent_vector, LATENT_SIZE, NUM_TRACKS, NUM_CLASSES)
    refiner_out = Refiner(generator_out, NUM_TRACKS, RESIDUAL_LAYERS, SLOPE_TENSOR)
    discriminiator_out = Discriminator(real_data, NUM_TRACKS)
    classifier_out = Classifier(real_data, NUM_TRACKS, NUM_CLASSES)

    #print("Real_data", real_data)
    print("\n\n")
    print("Generator out: ", generator_out)
    print("Refiner out: ", refiner_out)
    print("Discriminator out: ", discriminiator_out)
    print("Classifier out: ", classifier_out)
    print("\n\n")

    #TRAIN CLASSIFIER
    tf.reset_default_graph()

    #pass in data for session, assumes data labels will be passed in with data
    real_data = tf.placeholder(dtype = tf.bool, shape = [None, NUM_BARS, BEATS_PER_BAR, NUM_NOTES, NUM_TRACKS])
    real_data = tf.cast(real_data, dtype= tf.float32)
    real_data_labels = tf.placeholder(dtype = tf.int32, shape = None)

    #One hot encode data labels
    labels = tf.one_hot(real_data_labels, NUM_CLASSES)

    #Buld Classifier
    classifier_out = Classifier(real_data, NUM_TRACKS, NUM_CLASSES)

    #classifier_loss_functions = Loss_Functions(gp_coefficient = 1, discriminator_coefficient = 0.5)
    classifier_cce_loss = classifier_loss(classifier_out, labels, True)
    classifier_varlist = list(filter(lambda a : "classifier" in a.name, [v for v in tf.trainable_variables()]))
    optim = tf.train.AdamOptimizer(learning_rate=LEARNING_RATE, beta1=0.9, beta2=0.999, epsilon=1e-08, use_locking=False, name='Classifier_Optimizer').minimize(classifier_cce_loss, var_list=classifier_varlist)

    classifier_accuracy, acc_op = tf.metrics.accuracy(real_data_labels, tf.argmax(classifier_out, 1))

    print("Initialising session...")
    init_g = tf.global_variables_initializer()
    init_l = tf.local_variables_initializer()
    sess = tf.Session()
    sess.run(init_g)
    sess.run(init_l)
    saver = tf.train.Saver()

    data_path = abspath(sys.argv[1])
    print("Loading in Data from: ", data_path)
    data = Data(data_path) #Path to directory containing music set

    models_directory = join(os.getcwd(), "saved_models")
    os.makedirs(models_directory, exist_ok=True)
    accuracy_old = 0

    progress = trange(int(data.num_examples/BATCH_SIZE*CLASSIFIER_EPOCHS), desc = 'Bar_desc', leave = True)


    for t in progress:
        data_batch, label_batch = data.get_batch()
        loss_np, optim_np = sess.run([classifier_cce_loss, optim], feed_dict={real_data: data_batch, real_data_labels: label_batch})
        accuracy = sess.run([classifier_accuracy, acc_op], feed_dict={real_data: data_batch, real_data_labels: label_batch})
        progress.set_description(' LOSS ===> ' + str(loss_np) + ' ACCURACY ===> ' + str(accuracy[1]))
        progress.refresh()

        if (t*BATCH_SIZE)%data.num_examples == 0:
            if accuracy[1]>accuracy_old:
                accuracy_old = accuracy[1]
                #print("Saving File")
                filename = "model-" + str((t*BATCH_SIZE)/data.num_examples)+"-"+str(accuracy_old)
                saver.save(sess, join(models_directory, filename))



    def model_summary():
        model_vars = tf.trainable_variables()
        slim.model_analyzer.analyze_vars(model_vars, print_info=True)

    model_summary()

    layer_varlist = list(filter(lambda a : "classifier" in a.name, [v for v in tf.trainable_variables()]))
    print(layer_varlist)

    blah = tf.reshape(private_discriminator_out[-1, 4, 16, 7, 64])


if __name__ == '__main__':
    main()

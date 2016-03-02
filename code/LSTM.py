#! /usr/bin/python2

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import numpy as np
import time
import sys
from DataMan import DataMan
from hyperParams import *

# TensorFlow's API (if you want to know what arguments we pass to the different methods)
# https://www.tensorflow.org/versions/r0.7/api_docs/python/index.html

batch_counter = 0 # Keeps track of number of batches processed

class LSTM_Network(object):

    def __init__(self, data_set, keep_prob=1.0):
        initializer = tf.random_uniform_initializer(-init_range, init_range)
        max_word_seq = data_set.max_seq
        vocab_size = data_set.vocab_size

        # 2-dimensional tensors for input data and targets
        self._input = tf.placeholder(tf.int32, [batch_size, max_word_seq], name="input_data")
        self._target = tf.placeholder(tf.int64, [batch_size, max_word_seq], name="target_data")
        # This is the length of each sentence
        self._seq_lens = tf.placeholder(tf.int32, [batch_size], name="sequence_lengths")

        # Fetch word vectors
        with tf.device("/cpu:0"):
            embedding = tf.get_variable("embedding",
                    [vocab_size, embedding_size],
                    initializer=initializer)
            inputs = tf.nn.embedding_lookup(embedding, self._input)

        if keep_prob < 1:
            inputs = tf.nn.dropout(inputs, keep_prob)

        # Create the network
        cell = tf.nn.rnn_cell.BasicLSTMCell(hidden_layer_size)
        if keep_prob < 1:
            cell = tf.nn.rnn_cell.DropoutWrapper(cell, output_keep_prob=keep_prob)
        stacked_cells = tf.nn.rnn_cell.MultiRNNCell([cell] * number_of_layers)

        with tf.name_scope("initial_state"):
            self._initial_state = state = stacked_cells.zero_state(batch_size, tf.float32)

        with tf.name_scope("input"):
            # Give input the right shape
            inputs = [ tf.squeeze(input_, [1]) for input_ in tf.split(1, max_word_seq, inputs)]

        # Run through the whole batch and update state
        outputs, state = tf.nn.rnn(stacked_cells, inputs, initial_state=self._initial_state, sequence_length=self._seq_lens)

        with tf.name_scope("output"):
            # The output also needs some massaging
            output = tf.reshape(tf.concat(1, outputs), [-1, hidden_layer_size])

        z = None # Needed?
        with tf.name_scope("logits"):
            w = tf.get_variable("out_w", [hidden_layer_size, vocab_size], initializer=initializer)
            b = tf.get_variable("out_b", [vocab_size], initializer=initializer)
            z = tf.matmul(output, w) + b # Add supports broadcasting over each row

            # This is just to enable information for TensorBoard
            w_hist = tf.histogram_summary("weights", w)
            b_hist = tf.histogram_summary("biases", b)

        # Average negative log probability
        with tf.name_scope("cost"):
            with tf.name_scope("negative_log"):
                loss = tf.nn.sparse_softmax_cross_entropy_with_logits(z, tf.reshape(self._target, [-1]))

            with tf.name_scope("average"):
                self._cost = cost = tf.reduce_sum(loss) / batch_size
                ce_summ = tf.scalar_summary("cost", cost)

        self._final_state = state

        self._learning_rate = tf.Variable(learning_rate, trainable=False)
        # Gradient descent training op
        optimizer = tf.train.GradientDescentOptimizer(self._learning_rate)
        self._train_op = optimizer.minimize(self._cost)

    def set_learning_rate(self, sess, value):
        sess.run(tf.assign(self._learning_rate, value))

    def lr_decay_and_set(self, sess, epoch):
        if epoch > decay_start:
            decay = learning_decay ** (epoch - decay_start)
            self.set_learning_rate(sess, learning_rate * decay)

def run_epoch(sess, reader, net, merged, writer):
    total_cost = 0
    i = 0
    for i, (x, y, z) in enumerate(reader.batch_iterator(batch_size)):
        # Input
        feed = { net._input : x, net._target : y, net._seq_lens : z}
        # Run the computational graph
        cost, _ = sess.run([net._cost, net._train_op], feed_dict=feed) # Do we even have to run final state?
        total_cost += cost

        global batch_counter
        batch_counter += 1
        # Write information to TensorBoard log file
        if batch_counter % 10 == 0:
            summary_str = sess.run(merged, feed_dict=feed)
            writer.add_summary(summary_str, batch_counter)

    return total_cost / (i+1)

def save_state(sess, saver):
    print("Saving model.")
    save_path = saver.save(sess, "/tmp/model.ckpt")
    print("Model saved in file: {}".format(save_path))

def main():
    start_time = time.time()
    training_set = DataMan("train.txt")
    training_net = LSTM_Network(training_set, 1 - dropout)

    #validation_set = DataMan("valid.txt", training_set.get_dicts())
    #validation_net = LSTM_Network(validation_set)

    #validation_net = LSTM_Network(validation_set.vocab
    # We always need to run this operation before anything else
    init = tf.initialize_all_variables()
    # This operation will save our state at the end
    saver = tf.train.Saver()

    with tf.Session() as sess:
        # Initialize everything else
        sess.run(init)

        # Some initial stuff for TensorBoard
        merged = tf.merge_all_summaries()
        writer = tf.train.SummaryWriter("/tmp/tensorFlow_logs", sess.graph_def)

        print("Training.")
        for i in range(max_epoch):
            print("\r{}% done".format(int(i/max_epoch * 100)))
            training_net.lr_decay_and_set(sess, i)
            cost = run_epoch(sess, training_set, training_net, merged, writer)
        print("Finished training.")

        save_state(sess, saver)
        print("--- {} seconds ---".format(round(time.time() - start_time, 2)))

if __name__ == "__main__":
    main()


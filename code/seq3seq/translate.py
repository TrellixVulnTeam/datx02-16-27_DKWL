# Copyright 2015 Google Inc. All Rights Reserved.
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

"""Binary for training translation models and decoding from them.

Running this program without --decode will download the WMT corpus into
the directory specified as --data_dir and tokenize it in a very basic way,
and then start training a model saving checkpoints to --train_dir.

Running with --decode starts an interactive loop so you can see how
the current checkpoint translates English sentences into French.

See the following papers for more information on neural translation models.
 * http://arxiv.org/abs/1409.3215
 * http://arxiv.org/abs/1409.0473
 * http://arxiv.org/abs/1412.2007
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import os
import random
import sys
import time

import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf

import data_utils
import seq3seq_model


tf.app.flags.DEFINE_float("learning_rate", 0.5, "Learning rate.")
tf.app.flags.DEFINE_float("learning_rate_decay_factor", 0.99,
                          "Learning rate decays by this much.")
tf.app.flags.DEFINE_float("max_gradient_norm", 5.0,
                          "Clip gradients to this norm.")
tf.app.flags.DEFINE_integer("batch_size", 6,
                            "Batch size to use during training.")
tf.app.flags.DEFINE_integer("size", 300, "Size of each model layer.")
tf.app.flags.DEFINE_integer("num_layers", 3, "Number of layers in the model.")
tf.app.flags.DEFINE_integer("article_vocab_size", 20000, "Article vocabulary size.")
tf.app.flags.DEFINE_integer("title_vocab_size", 20000, "Title vocabulary size.")
tf.app.flags.DEFINE_string("data_dir", ".", "Data directory")
tf.app.flags.DEFINE_string("article_file", "articles.txt",
                           "file containing the articles (relative to data_dir)")
tf.app.flags.DEFINE_string("title_file", "titles.txt",
                           "file containing the titles (relative to data_dir)")
tf.app.flags.DEFINE_string("train_dir", "/tmp", "Training directory.")
tf.app.flags.DEFINE_integer("max_train_data_size", 0,
                            "Limit on the size of training data (0: no limit).")
tf.app.flags.DEFINE_integer("steps_per_checkpoint", 200,
                            "How many training steps to do per checkpoint.")
tf.app.flags.DEFINE_boolean("decode", False,
                            "Set to True for sample decoding.")
tf.app.flags.DEFINE_integer("max_sent", 250,
                            "How long the maximum sentence in an articel may be.")


FLAGS = tf.app.flags.FLAGS

# We use a number of buckets and pad to the closest one for efficiency.
# See seq3seq_model.Seq3SeqModel for details of how they work.
#
# Buckets are from the 100000 headline articles pairs in our small data set,
# they are very preliminary and we also opted to pad all titles since
# there's no apperent correlation between title and article lengths.
_buckets = [(1,48),(5, 48),(7,48),(10,48),(15,48),(20,48)]


def read_data(source_path, target_path, max_size=None):
  """Read data from source and target files and put into buckets.

  Args:
    source_path: path to the files with token-ids for the source language.
    target_path: path to the file with token-ids for the target language;
      it must be aligned with the source file: n-th line contains the desired
      output for n-th line from the source_path.
    max_size: maximum number of lines to read, all other will be ignored;
      if 0 or None, data files will be read completely (no limit).

  Returns:
    data_set: a list of length len(_buckets); data_set[n] contains a list of
      (source, target) pairs read from the provided data files that fit
      into the n-th bucket, i.e., such that len(source) < _buckets[n][0] and
      len(target) < _buckets[n][1]; source and target are lists of token-ids.
  """
  data_set = [[] for _ in _buckets]
  with tf.gfile.GFile(source_path, mode="r") as source_file:
    with tf.gfile.GFile(target_path, mode="r") as target_file:
      source, target = source_file.readline(), target_file.readline()
      counter = 0
      while source and target and (not max_size or counter < max_size):
        counter += 1
        if counter % 50000 == 0:
          print("  reading article %d" % counter)
          sys.stdout.flush()
        source_strings = source.split(" " + str(data_utils.EOS_ID) + " ")
        source_sents = [[int(x) for x in sent.split()] for sent in source_strings]
        
        # Put sentence length first and pad with zeros, the sentence lenght will be passed to tf.nn.rnn as sequnce_length
        source_ids = [[min(len(sent), FLAGS.max_sent)] +
                      sent[:FLAGS.max_sent] +
                      [0]*(FLAGS.max_sent - len(sent))
                      for sent in source_sents]
        target_ids = [int(x) for x in target.split()]
        target_ids.append(data_utils.EOS_ID)
        
        # Trim article to fit largest bucket that it doesn't fit into
        for bucket_id, (source_size, target_size) in sorted(enumerate(_buckets), key = lambda tup : tup[0], reverse=True):
          if len(source_ids) > source_size and len(target_ids) < target_size:
            data_set[bucket_id].append([source_ids[:source_size], target_ids])
            break
        source, target = source_file.readline(), target_file.readline()
      print("Data set read.")
  return data_set


def create_model(session, forward_only):
  """Create translation model and initialize or load parameters in session."""
  model = seq3seq_model.Seq3SeqModel(
      FLAGS.article_vocab_size, FLAGS.title_vocab_size, _buckets,
      FLAGS.size, FLAGS.num_layers, FLAGS.max_gradient_norm, FLAGS.batch_size,
      FLAGS.learning_rate, FLAGS.learning_rate_decay_factor,
      forward_only=forward_only)
  ckpt = tf.train.get_checkpoint_state(FLAGS.train_dir)
  if ckpt and tf.gfile.Exists(ckpt.model_checkpoint_path):
    print("Reading model parameters from %s" % ckpt.model_checkpoint_path)
    model.saver.restore(session, ckpt.model_checkpoint_path)
  else:
    print("Created model with fresh parameters.")
    session.run(tf.initialize_all_variables())
  return model


def train():
  """Train a article->title translation model using news data."""
  # Prepare news data.
  print("Preparing news data in %s" % FLAGS.data_dir)
  articles_train, titles_train, _, _ = data_utils.prepare_news_data(
      FLAGS.data_dir,
      FLAGS.article_file,
      FLAGS.title_file,
      FLAGS.article_vocab_size,
      FLAGS.title_vocab_size)

  with tf.Session() as sess:
    # Create model.
    print("Creating %d layers of %d units." % (FLAGS.num_layers, FLAGS.size))
    model = create_model(sess, False)

    # Read data into buckets and compute their sizes.
    print ("Reading development and training data (limit: %d)."
           % FLAGS.max_train_data_size)
    train_set = read_data(articles_train, titles_train, FLAGS.max_train_data_size)
    train_bucket_sizes = [len(train_set[b]) for b in xrange(len(_buckets))]
    train_total_size = float(sum(train_bucket_sizes))

    # A bucket scale is a list of increasing numbers from 0 to 1 that we'll use
    # to select a bucket. Length of [scale[i], scale[i+1]] is proportional to
    # the size if i-th training bucket, as used later.
    train_buckets_scale = [sum(train_bucket_sizes[:i + 1]) / train_total_size
                           for i in xrange(len(train_bucket_sizes))]

    # This is the training loop.
    step_time, loss = 0.0, 0.0
    current_step = 0
    previous_losses = []
    session_time = time.time()
    while True:
      # Choose a bucket according to data distribution. We pick a random number
      # in [0, 1] and use the corresponding interval in train_buckets_scale.
      random_number_01 = np.random.random_sample()

      bucket_id = min([i for i in xrange(len(train_buckets_scale))
                       if train_buckets_scale[i] > random_number_01])
      #print("Selected bucket %d" % bucket_id)

      # Get a batch and make a step.
      start_time = time.time()
      encoder_inputs, decoder_inputs, target_weights = model.get_batch(
          train_set, bucket_id)
      _, step_loss, _ = model.step(sess, encoder_inputs, decoder_inputs,
                                   target_weights, bucket_id, False)
      step_time += (time.time() - start_time) / FLAGS.steps_per_checkpoint
      loss += step_loss / FLAGS.steps_per_checkpoint
      current_step += 1

      # Once in a while (if more than one hour has passed), we save checkpoint, print statistics, and run evals.
      if (time.time()- session_time > 3600):
        # Print statistics for the previous epoch.
        perplexity = math.exp(loss) if loss < 300 else float('inf')
        print ("global step %d learning rate %.4f step-time %.2f perplexity "
               "%.2f" % (model.global_step.eval(), model.learning_rate.eval(),
                         step_time, perplexity))
        # Decrease learning rate if no improvement was seen over last 3 times.
        if len(previous_losses) > 2 and loss > max(previous_losses[-3:]):
          sess.run(model.learning_rate_decay_op)
        previous_losses.append(loss)
        # Save checkpoint and zero timer and loss.
        checkpoint_path = os.path.join(FLAGS.train_dir, "translate.ckpt")
        model.saver.save(sess, checkpoint_path, global_step=model.global_step)
        step_time, loss = 0.0, 0.0
        sys.stdout.flush()
        break


def decode():
  with tf.Session() as sess:
    # Create model and load parameters.
    model = create_model(sess, True)
    model.batch_size = 1  # We decode one sentence at a time.

    # Load vocabularies.
    article_vocab_path = os.path.join(FLAGS.data_dir,
                                 "vocab%d.article" % FLAGS.article_vocab_size)
    title_vocab_path = os.path.join(FLAGS.data_dir,
                                 "vocab%d.title" % FLAGS.title_vocab_size)
    article_vocab, _ = data_utils.initialize_vocabulary(article_vocab_path)
    _, rev_title_vocab = data_utils.initialize_vocabulary(title_vocab_path)

    # Decode from three random article.
    sys.stdout.write("Decide headline for three articles:")
    sys.stdout.flush()
    articles = random.sample(list(open(os.path.join(FLAGS.data_dir, FLAGS.article_file))),50)
    for article in articles:
      # Get token-ids for the input sentence.
      token_ids = data_utils.sentence_to_token_ids(article, article_vocab)
      # Which bucket does it belong to?
      bucket_id = min([b for b in xrange(len(_buckets))
                       if _buckets[b][0] > len(token_ids)])
      # Get a 1-element batch to feed the sentence to the model.
      encoder_inputs, decoder_inputs, target_weights = model.get_batch(
          {bucket_id: [(token_ids, [])]}, bucket_id)
      # Get output logits for the sentence.
      _, _, output_logits = model.step(sess, encoder_inputs, decoder_inputs,
                                       target_weights, bucket_id, True)
      # This is a greedy decoder - outputs are just argmaxes of output_logits.
      outputs = [int(np.argmax(logit, axis=1)) for logit in output_logits]
      # If there is an EOS symbol in outputs, cut them at that point.
      if data_utils.EOS_ID in outputs:
        outputs = outputs[:outputs.index(data_utils.EOS_ID)]
      # Print out title corresponding to outputs.
      print("-"*80)
      print(" ".join([rev_title_vocab[output] for output in outputs]))
      print("-"*80)
      print(article)
      print("-"*80)

      sys.stdout.flush()

def main(_):
  if FLAGS.decode:
    decode()
  else:
    train()

if __name__ == "__main__":
  tf.app.run()
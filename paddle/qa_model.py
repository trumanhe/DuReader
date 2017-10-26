# -*- coding:utf8 -*-
###############################################################################
#
# Copyright (c) 2017 Baidu.com, Inc. All Rights Reserved
#
###############################################################################
"""
This module implements the basic common functions of the Match-LSTM and BiDAF
networks.

Authors: liuyuan(liuyuan04@baidu.com)
Data: 2017/09/20 12:00:00
"""
import json
import logging
import paddle.v2.layer as layer
import paddle.v2.attr as Attr
import paddle.v2.activation as Act
import paddle.v2.data_type as data_type
import paddle.v2 as paddle
import squad_eval
from brc_eval import compute_metrics_from_list

logger = logging.getLogger("paddle")
logger.setLevel(logging.INFO)

class QAModel(object):
    """
    This is the base class of Match-LSTM and BiDAF models.
    """
    def __init__(self, name, inputs, *args, **kwargs):
        self.name = name
        self.inputs = inputs
        self.emb_dim = kwargs['emb_dim']
        self.vocab_size = kwargs['vocab_size']
        self.is_infer = kwargs['is_infer']
        self.doc_num = kwargs['doc_num']
        self.static_emb = kwargs['static_emb']
        self.metric = kwargs['metric']

    def check_and_create_data(self):
        """
        Checks if the input data is legal and creates the data layers
        according to the input fields.
        """
        if self.is_infer:
            expected = ['q_ids', 'p_ids', 'para_length',
                        '[start_label, end_label, ...]']
            if len(self.inputs) < 3:
                raise ValueError(r'''Input schema: expected vs given:
                         {} vs {}'''.format(expected, self.inputs))
        else:
            expected = ['q_ids', 'p_ids', 'para_length',
                        'start_label', 'end_label', '...']
            if len(self.inputs) < 5:
                raise ValueError(r'''Input schema: expected vs given:
                         {} vs {}'''.format(expected, self.inputs))
            self.start_labels = []
            for i in range(1 + 2 * self.doc_num, 1 + 3 * self.doc_num):
                self.start_labels.append(
                        layer.data(name=self.inputs[i],
                            type=data_type.dense_vector_sequence(1)))
            self.start_label = reduce(
                    lambda x, y: layer.seq_concat(a=x, b=y),
                    self.start_labels)
            self.end_labels = []
            for i in range(1 + 3 * self.doc_num, 1 + 4 * self.doc_num):
                self.end_labels.append(
                        layer.data(name=self.inputs[i],
                            type=data_type.dense_vector_sequence(1)))
            self.end_label = reduce(
                    lambda x, y: layer.seq_concat(a=x, b=y),
                    self.end_labels)
        self.q_ids = layer.data(
                name=self.inputs[0],
                type=data_type.integer_value_sequence(self.vocab_size))
        self.p_ids = []
        for i in range(1, 1 + self.doc_num):
            self.p_ids.append(
                    layer.data(name=self.inputs[i],
                        type=data_type.integer_value_sequence(self.vocab_size)))
        self.para_lens = []
        for i in range(1 + self.doc_num, 1 + 2 * self.doc_num):
            self.para_lens.append(
                    layer.data(name=self.inputs[i],
                        type=data_type.dense_vector_sequence(1)))
        self.para_len = reduce(lambda x, y: layer.seq_concat(a=x, b=y),
                self.para_lens)

    def create_shared_params(self):
        """
        Creates parameter objects that shared by multiple layers.
        """
        # embedding parameter, shared by question and paragraph.
        self.emb_param = Attr.Param(name=self.name + '.embs',
                                    is_static=self.static_emb)

    def get_embs(self, input):
        """
        Get embeddings of token sequence.
        Args:
            - input: input sequence of tokens. Should be of type
                     paddle.v2.data_type.integer_value_sequence
        Returns:
            The sequence of embeddings.
        """
        embs = layer.embedding(input=input,
                               size=self.emb_dim,
                               param_attr=self.emb_param)
        return embs

    def network(self):
        """
        Implements the detail of the model. Should be implemented by subclasses.
        """
        raise NotImplementedError

    def get_loss(self, start_prob, end_prob, start_label, end_label):
        """
        Compute the loss: $l_{\theta} = -logP(start)\cdotP(end|start)$

        Returns:
            A LayerOutput object containing loss.
        """
        probs = layer.seq_concat(a=start_prob, b=end_prob)
        labels = layer.seq_concat(a=start_label, b=end_label)

        log_probs = layer.mixed(
                    size=probs.size,
                    act=Act.Log(),
                    bias_attr=False,
                    input=paddle.layer.identity_projection(probs))

        neg_log_probs = layer.slope_intercept(
                        input=log_probs,
                        slope=-1,
                        intercept=0)

        loss = paddle.layer.mixed(
               size=1,
               input=paddle.layer.dotmul_operator(a=neg_log_probs, b=labels))

        sum_val = paddle.layer.pooling(input=loss,
                                       pooling_type=paddle.pooling.Sum())
        cost = paddle.layer.sum_cost(input=sum_val)
        return cost

    def train(self):
        """
        The training interface.

        Returns:
            A LayerOutput object containing loss.
        """
        start, end = self.network()
        cost = self.get_loss(start, end, self.start_label, self.end_label)
        return cost

    def infer(self):
        """
        The inferring interface.

        Returns:
            start_end: A sequence of concatenated start and end probabilities.
            para_len: A sequence of the lengths of every paragraph, which is
                      used for parse the inferring output.
        """
        start, end = self.network()
        start_end = layer.seq_concat(name='start_end', a=start, b=end)
        return start_end, self.para_len

    def decode(self, name, input):
        """
        Implements the answer pointer part of the model.

        Args:
            name: name prefix of the layers defined in this method.
            input: the encoding of the paragraph.

        Returns:
            A probability distribution over temporal axis.
        """
        latent = layer.fc(size=input.size / 2,
                          input=input,
                          act=Act.Tanh(),
                          bias_attr=False)
        probs = layer.fc(
                name=name,
                size=1,
                input=latent,
                act=Act.SequenceSoftmax())
        return probs

    def __parse_infer_ret(self, infer_ret):
        doc_num = 5 if self.metric == 'marco' else 1
        pred_list = []
        ref_list = []
        objs = []
        ins_cnt = 0
        for batch_input, batch_output in infer_ret:
            lens, probs = [x.flatten() for x in batch_output]
            len_sum = int(sum(lens))
            assert len(probs) == 2 * len_sum
            idx_len = 0
            idx_prob = 0

            for ins in batch_input:
                ins = ins[-1]
                len_slice = lens[idx_len:idx_len + doc_num]
                prob_len = int(sum(len_slice))
                start_prob_slice = probs[idx_prob:idx_prob + prob_len]
                end_prob_slice = probs[idx_prob + prob_len:idx_prob + 2 * prob_len]
                start_idx = start_prob_slice.argmax(axis=0)
                if start_idx < prob_len - 1:
                    rest_slice = end_prob_slice[start_idx:]
                    end_idx = start_idx + rest_slice.argmax(axis=0)
                else:
                    end_idx = start_idx
                pred_tokens = [] if start_idx > end_idx \
                        else ins['tokens'][start_idx:end_idx + 1]
                pred = ' '.join(pred_tokens)
                ref = ins['answer']
                idx_len += doc_num
                idx_prob += prob_len * 2
                pred_obj = {'answer': [pred],
                        'query': ins_cnt,
                        'question': ins['question']}
                ref_obj = {'answer': ref,
                        'query': ins_cnt,
                        'question': ins['question']}
                stored_obj = {'question': ins['question'],
                        'query': ins_cnt,
                        'answer_ref': ref,
                        'answer_pred': [pred]}
                objs.append(stored_obj)
                pred_list.append(pred_obj)
                ref_list.append(ref_obj)
                ins_cnt += 1
        return ref_list, pred_list, objs

    def __read_list(self, infer_file):
        ref_list = []
        pred_list = []
        with open(infer_file, 'r') as inf:
            for line in inf:
                obj = json.loads(line.strip())
                ref_obj = {'query': obj['query'], 'answer': obj['answer_ref']}
                pred_obj = {'query': obj['query'], 'answer': obj['answer_pred']}
                ref_list.append(ref_obj)
                pred_list.append(pred_obj)
        return ref_list, pred_list

    def evaluate(self,
            infer_file,
            ret=None,
            from_file=False):
        """
        Processes and evaluates the inferred result.

        Args:
            infer_file: A file name to store or read from the inferred results.
            ret: The information returned by the inferring operation, which
                 contains the batch-level input and the the batch-level
                 inferring result.
            from_file: If True, the time consuming inferring process will be
                       skipped, and this method takes the content of infer_file
                       as input for evaluation. If False, this method takes
                       the ret as input for evaluation.

        """
        pred_list = []
        ref_list = []
        objs = []

        if from_file:
            ref_list, pred_list = self.__read_list(infer_file)
        else:
            ref_list, pred_list, objs = self.__parse_infer_ret(ret)
            with open(infer_file, 'w') as of:
                for o in objs:
                    print >> of, json.dumps(o, ensure_ascii=False).encode('utf8')
        if self.metric == 'marco':
            metrics = compute_metrics_from_list(pred_list, ref_list, 1)
        elif self.metric == 'squad':
            metrics = squad_eval.eval_lists(pred_list, ref_list)
        else:
            raise ValueError("Unknown metrics '{}'".format(self.metric))
        res_str = '{} {}'.format(infer_file,
                ' '.join('{}={}'.format(k, v) for k, v in metrics.items()))
        logger.info(res_str)

    def __call__(self):
        if self.is_infer:
            return self.infer()
        return self.train()
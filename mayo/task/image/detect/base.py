import numpy as np
import tensorflow as tf

from mayo.util import Percent
from mayo.task.image.base import ImageTaskBase
from mayo.task.image.detect import util


class ImageDetectTaskBase(ImageTaskBase):
    """
    This base class implements mAP evaluation for all detection algorithms.
    """
    def _mean_avg_precisions(
            self, pred_box, pred_class, pred_score, pred_count,
            truth_box, truth_class, truth_count):
        num_imgs, num_classes = self.batch_size, self.num_classes
        detections = [[None for i in range(num_classes)] for j in
                      range(num_imgs)]
        annotations = [[None for i in range(num_classes)] for j in
                       range(num_imgs)]

        for i in range(num_imgs):
            pred_box_i = pred_box[i, :]
            pred_score_i = np.array([pred_score[i, :]])
            truth_box_i = truth_box[i, :]
            pred_class_i = pred_class[i, :]
            for label in range(num_classes):
                index = pred_class_i == label
                if pred_count[i] == 0 or (not any(index)):
                    detections[i][label] = []
                else:
                    detections[i][label] = np.concatenate(
                        (pred_box_i[index[:], :],
                         pred_score_i[:, index[:]].T), axis=1)
                index = truth_class[i, :] == label
                if truth_count[i] == 0 or (not any(index)):
                    annotations[i][label] = []
                else:
                    annotations[i][label] = truth_box_i[index, :]
        # compute mean_aps
        avg_precisions = np.zeros((num_classes, 1))
        for label in range(num_classes):
            false_pos = true_pos = scores = np.zeros((0,))
            num_annotations = 0.0
            for i in range(num_imgs):
                ds = detections[i][label]
                ans = annotations[i][label]
                num_annotations += ans.shape[0] if ans != [] else 0
                detected_ans = []
                for d in ds:
                    scores = np.append(scores, d[4])
                    if ans == [] or ans.shape[0] == 0:
                        false_pos = np.append(false_pos, 1)
                        true_pos = np.append(true_pos, 0)
                        continue
                    overlaps, iw, ih, intersection, ou = util.np_iou(
                        np.expand_dims(d[:4], axis=0), ans)
                    tmp = np.expand_dims(d[:4], axis=0)
                    assigned_ans = np.argmax(overlaps, axis=1)
                    max_overlap = overlaps[0, assigned_ans]
                    if max_overlap >= self.iou_threshold \
                            and assigned_ans not in detected_ans:
                        false_pos = np.append(false_pos, 0)
                        true_pos = np.append(true_pos, 1)
                        detected_ans.append(assigned_ans)
                    else:
                        false_pos = np.append(false_pos, 1)
                        true_pos = np.append(true_pos, 0)
            if num_annotations == 0:
                avg_precisions[label] = 0
                continue

            # sort by score
            indices = np.argsort(-scores)
            false_pos = false_pos[indices[:]]
            true_pos = true_pos[indices[:]]

            # compute false positives and true positives
            false_pos = np.cumsum(false_pos)
            true_pos = np.cumsum(true_pos)

            # compute recall and precision
            recall = true_pos / num_annotations
            precision = true_pos / np.maximum(
                true_pos + false_pos, np.finfo(np.float64).eps)

            # compute average precision
            ap = util.np_average_precision(recall, precision)
            avg_precisions[label] = ap
        return avg_precisions.astype(np.float32)

    def eval(self, net, prediction, truth):
        """
            detections: the prediciton of object class
            annotations: positions of the objects
        """
        # forced dummy test
        # fakes = []
        # for item in mean_aps_inputs:
        #     fakes.append(np.random.randint(10, size=item.shape))
        # dummy = self._mean_avg_precisions(*fakes)
        inputs = [
            prediction['test']['box'],
            prediction['test']['class'],
            prediction['test']['score'],
            prediction['test']['count'],
            truth['rawbox'],
            truth['rawclass'],
            truth['count'],
        ]
        tensor = tf.py_func(self._mean_avg_precisions, inputs, tf.float32)
        tensor = tf.reduce_sum(tensor)
        tensor /= tf.cast(tf.size(tensor), tf.float32)
        formatter = lambda e: 'mAP accuracy: {}'.format(
            Percent(e.get_mean('accuracy')))
        self.estimator.register(tensor, 'accuracy', formatter=formatter)

import re
import copy

from mayo.log import log
from mayo.util import memoize_property
from mayo.session.train import Train


class SearchBase(Train):
    def _profile(self):
        baseline = self.config.search.accuracy.get('baseline')
        if baseline:
            return baseline
        self.reset_num_epochs()
        log.info('Profiling baseline accuracy...')
        total_accuracy = step = epoch = 0
        while epoch < self.config.search.profile_epochs:
            epoch = self.run([self.num_epochs], batch=True)
            total_accuracy += self.estimator.get_value('accuracy', 'train')
            step += 1
        self.baseline = total_accuracy / step
        tolerance = self.config.search.accuracy.tolerance
        self.tolerable_baseline = self.baseline * tolerance
        log.info(
            'Baseline accuracy: {}, tolerable accuracy: .'
            .format(self.baseline, self.tolerable_baseline))
        self.reset_num_epochs()

    def search(self):
        # profile training accuracy for a given number of epochs
        self._profile()
        # initialize search
        self._init_search()
        # main procedure
        max_steps = self.config.search.max_steps
        step = 0
        blacklist = set()
        while True:
            if max_steps and step > max_steps:
                break
            step += 1
            if not self._kernel(blacklist):
                break
        log.info('Automated hyperparameter optimization done.')

    def _init_targets(self):
        # intialize target hyperparameter variables to search
        targets = {}
        for regex, info in self.config.search.variables.items():
            for node, node_variables in self.variables.items():
                for name, var in node_variables.items():
                    if not re.search(regex, name):
                        continue
                    if node in targets:
                        raise ValueError(
                            'We are currently unable to handle multiple '
                            'hyperparameter variables within the same layer.')
                    targets[node] = dict(info, variable=var)
                    log.debug(
                        'Targeted hyperparameter {} in {}: {}.'
                        .format(var, node.formatted_name(), info))
        return targets


class LayerSearch(SearchBase):
    def _init_search(self):
        self.targets = self._init_targets()
        self.backtrack_targets = None
        # initialize # hyperparameters to starting positions
        # FIXME how can we continue search?
        for _, info in self.targets:
            start = info['from']
            var = info['variable']
            # unable to use overrider-based hyperparameter assignment, but it
            # shouldn't be a problem
            self.assign(var, start)
        # save a starting checkpoint for backtracking
        self.save_checkpoint('backtrack')

    def _priority(self, blacklist=None):
        key = self.config.search.cost_key
        info = self.task.nets[0].estimate()
        priority = []
        for node, stats in info.items():
            if node not in self.targets:
                continue
            if blacklist and node in blacklist:
                continue
            priority.append((node, stats[key]))
        return list(reversed(sorted(priority, key=lambda v: v[1])))

    def backtrack(self):
        if not self.backtrack_targets:
            return False
        self.targets = self.backtrack_targets
        self.load_checkpoint('backtrack')
        return True

    def set_backtrack_to_here(self):
        self.backtrack_targets = copy.deepcopy(self.targets)
        self.save_checkpoint('backtrack')

    def fine_tune(self):
        self.reset_num_epochs()
        max_finetune_epoch = self.config.search.max_epochs.fine_tune
        total_accuracy = step = epoch = 0
        while epoch < max_finetune_epoch:
            epoch, _ = self.run([self.num_epochs, self.train_op])
            total_accuracy += self.estimator.get_value('accuracy', 'train')
            step += 1
        return total_accuracy / step

    def _step_forward(self, value, end, step, min_step):
        new_value = value + step
        if step > 0 and new_value > end or step < 0 and new_value < end:
            # step size is too large, half it
            new_step = step / 2
            if new_step < min_step:
                # cannot step further
                return False
            return self._step_forward(value, end, step / 2, min_step)
        return new_value

    def _kernel(self, blacklist):
        priority = self._priority(blacklist)
        if not priority:
            log.debug('All nodes blacklisted.')
            return False
        node = priority[0]
        info = self.targets[node]
        node_name = node.formatted_name()
        log.debug('Prioritize layer {!r}.'.format(node_name))
        value = self._step_forward(
            info['from'], info['to'], info['step'], info['min_step'])
        var = info['variable']
        if value is False:
            log.debug(
                'Blacklisting {!r} as we cannot further '
                'increment/decrement {!r}.'.format(node_name, var))
            blacklist.add(node)
            return True
        self.assign(var, value)
        info['from'] = value
        log.info(
            'Updated hyperparameter {} in layer {!r} with a new value {}.'
            .format(var, node_name, value))
        # fine-tuning with updated hyperparameter
        accuracy = self.fine_tune()
        if accuracy >= self.tolerable_baseline:
            log.debug(
                'Fine-tuned accuracy {!r} found tolerable.'
                .format(accuracy))
            self.set_backtrack_to_here()
            return True
        new_step = info['step'] / 2
        if new_step < info['min_step']:
            blacklist.add(node)
            log.debug(
                'Blacklisting {!r} as we cannot use smaller '
                'increment/decrement.'.format(node_name))
            return True
        self.backtrack()
        info['step'] = new_step
        return True


class GlobalSearch(SearchBase):
    def _init_search(self):
        self.targets = self._init_targets()
        self.backtrack_targets = None
        # initialize # hyperparameters to starting positions
        # FIXME how can we continue search?
        steps = []
        for _, info in self.targets:
            start = info['from']
            var = info['variable']
            # unable to use overrider-based hyperparameter assignment, but it
            # shouldn't be a problem
            steps.append(info['from'])
            self.assign(var, start)
        # check steps
        if not all(x == steps[0] for x in steps):
            log.error('All step values must be equal in global search, '
                      'but we found {}'.format(steps))
        # save a starting checkpoint for backtracking
        self.save_checkpoint('backtrack')

    def _kernel(self, blacklist):
        for node, info in self.targets.items():
            node_name = node.formatted_name()
            value = self._step_forward(
                info['from'], info['to'], info['step'], info['min_step'])
            var = info['variable']
            if value is False:
                log.debug(
                    'Stop becase of {!r}, we cannot further '
                    'increment/decrement {!r}.'.format(node_name, var))
                return False
            self.assign(var, value)
            info['from'] = value
            log.info(
                'Updated hyperparameter {} in layer {!r} with a new value {}.'
                .format(var, node_name, value))
        # fine-tuning with updated hyperparameter
        accuracy = self.fine_tune()
        if accuracy >= self.tolerable_baseline:
            log.debug(
                'Fine-tuned accuracy {!r} found tolerable.'
                .format(accuracy))
            self.set_backtrack_to_here()
            return True
        new_step = info['step'] / 2
        if new_step < info['min_step']:
            log.debug(
                'Stop because of {!r}, as we cannot use smaller '
                'increment/decrement.'.format(node_name))
            return False
        self.backtrack()
        info['step'] = new_step
        return True

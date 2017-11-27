import argparse, json, math, time, pdb
from pprint import pprint
from box import Box
import numpy as np
import pandas as pd
import tensorflow as tf
from sqlalchemy.sql import text
from tensorforce.agents import agents as agents_dict
from tensorforce.core.networks.layer import Dense
from tensorforce.core.networks.network import LayeredNetwork
from tensorforce.contrib.openai_gym import OpenAIGym
from tensorforce.environments import Environment
from tensorforce.execution import Runner

"""
Each hyper is specified as `key: {type, vals, requires, hook}`. 
- type: (int|bounded|bool). bool is True|False param, bounded is a float between min & max, int is "choose one" 
    eg 'activation' one of (tanh|elu|selu|..)`)
- vals: the vals this hyper can take on. If type(vals) is primitive, hard-coded at this value. If type is list, then
    (a) min/max specified inside (for bounded); (b) all possible options (for 'int'). If type is dict, then the keys
    are used in the searching (eg, look at the network hyper) and the values are used as the configuration.
- requires: can specify that a hyper should be deleted if some other hyper (a) doesn't exist (type(requires)==str), 
    (b) doesn't equal some value (type(requires)==dict)
- hook: transform this hyper before plugging it into Configuration(). Eg, we'd use type='bounded' for batch size since
    we want to range from min to max (insteaad of listing all possible values); but we'd cast it to an int inside
    hook before using it. (Actually we clamp it to blocks of 8, as you'll see)      

The special sauce is specifying hypers as dot-separated keys, like `memory.type`. This allows us to easily 
mix-and-match even within a config-dict. Eg, you can try different combos of hypers within `memory{}` w/o having to 
specify the whole block combo (`memory=({this1,that1}, {this2,that2})`). To use this properly, make sure to specify
a `requires` field where necessary. 
"""


def build_net_spec(net):
    """Builds a net_spec from some specifications like width, depth, etc"""
    net = Box(net)
    dense = {'type': 'dense', 'activation': net.activation, 'l2_regularization': net.l2}  # , 'l1_regularization': l1}
    dropout = {'type': 'dropout', 'rate': net.dropout}
    conv2d = {'type': 'conv2d', 'bias': True}  # TODO bias as hyper?
    lstm = {'type': 'lstm', 'dropout': net.dropout}

    arr = []
    if net.dropout:
        arr.append({**dropout})

    # Pre-layer
    if 'pre_depth' in net:
        for i in range(net.pre_depth):
            size = int(net.width/(net.pre_depth-i+1)) if net.funnel else net.width
            arr.append({'size': size, **dense})
            if net.dropout: arr.append({**dropout})

    # Mid-layer
    for i in range(net.depth):
        if net.type == 'conv2d':
            size = max([32, int(net.width/4)])
            if i == 0: size = int(size/2) # FIXME most convs have their first layer smaller... right? just the first, or what?
            arr.append({'size': size, 'window': (8, net.window), 'stride': (1, net.stride), **conv2d})
        else:
            size = net.width
            arr.append({'size': size, **lstm})
    if net.type == 'conv2d':
        arr.append({'type': 'flatten'})

    # Dense
    for i in range(net.depth):
        size = int(net.width / (i + 2)) if net.funnel else net.width
        arr.append({'size': size, **dense})
        if net.dropout: arr.append({**dropout})

    return arr


def bins_of_8(x): return int(x // 8) * 8


hypers = {}
hypers['agent'] = {
    # TODO preprocessing, batch_observe, reward_preprocessing[dict(type='clip', min=-1, max=1)]
}
hypers['batch_agent'] = {
    'batch_size': {
        'type': 'bounded',
        'vals': [8, 256],
        'hook': bins_of_8
    },
    'keep_last_timestep': {
        'type': 'bool'
    }
}
hypers['model'] = {
    'optimizer.type': {
        'type': 'int',
        'vals': ['nadam', 'adam'],  # TODO rmsprop
    },
    'optimizer.learning_rate': {
        'type': 'bounded',
        'vals': [1e-7, 1e-1],
    },
    'optimization_steps': {
        'type': 'bounded',
        'vals': [2, 20],
        'hook': int
    },
    'discount': {
        'type': 'bounded',
        'vals': [.95, .99],
    },
    'normalize_rewards': {
        # True is definite winner
        'type': 'bool'
    },
    # TODO variable_noise
}
hypers['distribution_model'] = {
    'entropy_regularization': {
        'type': 'bounded',
        'vals': [0., 1.],
    }
    # distributions_spec (gaussian, beta, etc). Pretty sure meant to handle under-the-hood, investigate
}
hypers['pg_model'] = {
    'baseline_mode': 'states',
    'gae_lambda': {
        'requires': 'baseline_mode',
        'type': 'bounded',
        'vals': [.94, .99],
        'hook': lambda x: None if x < .95 else x
    },
    'baseline_optimizer.optimizer.learning_rate': {
        'requires': 'baseline_mode',
        'type': 'bounded',
        'vals': [1e-7, 1e-1]
    },
    'baseline_optimizer.num_steps': {
        'requires': 'baseline_mode',
        'type': 'bounded',
        'vals': [2, 20],
        'hook': int
    },
}
hypers['pg_prob_ration_model'] = {
    # I don't know what values to use besides the defaults, just guessing. Look into
    'likelihood_ratio_clipping': {
        'type': 'bounded',
        'vals': [0., 1.],
    }
}

hypers['ppo_agent'] = {  # vpg_agent, trpo_agent
    **hypers['agent'],
    **hypers['batch_agent'],
    **hypers['model'],
    **hypers['distribution_model'],
    **hypers['pg_model'],
    **hypers['pg_prob_ration_model']

}
hypers['ppo_agent']['step_optimizer.learning_rate'] = hypers['ppo_agent'].pop('optimizer.learning_rate')
hypers['ppo_agent']['step_optimizer.type'] = hypers['ppo_agent'].pop('optimizer.type')

hypers['custom'] = {
    'net.type': {
        'type': 'int',
        'vals': ['conv2d', 'lstm']
    },
    'net.window': {
        'type': 'bounded',
        'vals': [3, 8],
        'hook': int,
        'requires': {'net.type': 'conv2d'}
    },
    'net.stride': {
        'type': 'bounded',
        'vals': [1, 4],
        'hook': int,
        'requires': {'net.type': 'conv2d'}
    },
    'net.pre_depth': {
        'type': 'bounded',
        'vals': [0, 3],
        'hook': int,
        'requires': {'net.type': 'lstm'}
    },
    'net.depth': {
        'type': 'bounded',
        'vals': [1, 5],
        'hook': int
    },
    'net.width': {
        'type': 'bounded',
        'vals': [32, 768],
        'hook': bins_of_8
    },
    'net.funnel': {
        'type': 'bool'
    },
    'net.activation': {
        'type': 'int',
        'vals': ['tanh', 'elu', 'relu', 'selu'],
    },
    'net.dropout': {
        'type': 'bounded',
        'vals': [0., .5],
        'hook': lambda x: None if x < .1 else x
    },
    'net.l2': {
        'type': 'bounded',
        'vals': [1e-5, 1e-1]
    },
}

# Fill in implicit 'vals' (eg, 'bool' with [True, False])
for _, section in hypers.items():
    for k, v in section.items():
        if type(v) != dict: continue  # hard-coded vals
        if v['type'] == 'bool': v['vals'] = [0, 1]

class DotDict(object):
    """
    Utility class that lets you get/set attributes with a dot-seperated string key, like `d = a['b.c.d']` or `a['b.c.d'] = 1`
    """
    def __init__(self, data):
        self._data = data

    def __getitem__(self, path):
        v = self._data
        for k in path.split('.'):
            if k not in v:
                return None
            v = v[k]
        return v

    def __setitem__(self, path, val):
        v = self._data
        path = path.split('.')
        for i, k in enumerate(path):
            if i == len(path) - 1:
                v[k] = val
                return
            elif k in v:
                v = v[k]
            else:
                v[k] = {}
                v = v[k]

    def to_dict(self):
        return self._data


class HSearchEnv(object):
    """
    This is the "wrapper" environment (the "inner" environment is the one you're testing against, like Cartpole-v0).
    This env's actions are all the hyperparameters (above). The state is nothing (`[1.]`), and a single episode is
    running the inner-env however many episodes (300). The inner's last-few reward avg is outer's one-episode reward.
    That's one run: make inner-env, run 300, avg reward, return that. The next episode will be a new set of
    hyperparameters (actions); run inner-env from scratch using new hypers.
    """
    def __init__(self, agent='ppo_agent'):
        """
        TODO only tested with ppo_agent. There's some code for dqn_agent, but I haven't tested. Nothing else
        is even attempted implemtned
        """
        hypers_ = hypers[agent].copy()
        hypers_.update(hypers['custom'])

        self.agent = agent
        self.hypers = hypers_
        self.hardcoded = {}

    def _action2val(self, k, v):
        # from TensorForce, v is a numpy object - unpack. From Bayes, it's a primitive. TODO handle better
        try: v = v.item()
        except Exception: pass

        hyper = self.hypers[k]
        if 'hook' in hyper:
            v = hyper['hook'](v)
        if hyper['type'] == 'bool': return bool(round(v))
        if hyper['type'] == 'int':
            if type(hyper['vals']) == list and type(v) == int:
                return hyper['vals'][v]
            # Else it's a dict. Don't map the values till later (keep them as keys in flat)
        return v

    def _key2val(self, k, v):
        hyper = self.hypers[k]
        if type(hyper) == dict and type(hyper.get('vals', None)) == dict:
            return hyper['vals'][v]
        return v

    def get_hypers(self, actions):
        """
        Bit of confusing logic here where I construct a `flat` dict of hypers from the actions - looks like how hypers
        are specified above ('dot.key.str': val). Then from that we hydrate it as a proper config dict (hydrated).
        Keeping `flat` around because I save the run to the database so can be analyzed w/ a decision tree
        (for feature_importances and the like) and that's a good format, rather than a nested dict.
        :param actions: the hyperparamters
        """
        flat = {k: self._action2val(k, v) for k, v in actions.items()}
        flat.update(self.hardcoded)
        self.flat = flat

        # Ensure dependencies (do after above to make sure the randos have "settled")
        for k in list(flat):
            if k in self.hardcoded: continue
            hyper = self.hypers[k]
            if not (type(hyper) is dict and 'requires' in hyper): continue
            req = hyper['requires']
            # Requirement is a string (require the value's not None). TODO handle nested deps.
            if type(req) is str:
                if not flat[req]: del flat[k]
                continue
            # Requirement is a dict of type {key: value_it_must_equal}. TODO handle multiple deps
            dep_k, dep_v = list(req.items())[0]
            if flat[dep_k] != dep_v:
                del flat[k]

        session_config = None if self.gpu_split == 1 else \
            tf.ConfigProto(gpu_options=tf.GPUOptions(per_process_gpu_memory_fraction=.82 / self.gpu_split))
        # TODO put this in hard-coded hyper above?
        if self.agent == 'ppo_agent':
            hydrated = DotDict({
                'session_config': session_config,
                'baseline_mode': 'states',
                'baseline': {'type': 'custom'},
                'baseline_optimizer': {'type': 'multi_step', 'optimizer': {'type': flat['step_optimizer.type']}},
            })
        else:
            hydrated = DotDict({'session_config': session_config})

        # change all a.b=c to {a:{b:c}} (note DotDict class above, I hate and would rather use an off-the-shelf)
        for k, v in flat.items():
            if k not in hypers['custom']:
                hydrated[k] = self._key2val(k, v)
        hydrated = hydrated.to_dict()

        extra = DotDict({})
        for k, v in flat.items():
            if k in hypers['custom']:
                extra[k] = self._key2val(k, v)
        extra = extra.to_dict()
        network = build_net_spec(extra['net'])

        if flat.get('baseline_mode', None):
            baseline_net = build_net_spec(extra['net'])
            if flat['net.type'] == 'lstm':
                baseline_net = [l for l in baseline_net if l['type'] != 'lstm']
            hydrated['baseline']['network_spec'] = baseline_net

        print('--- Flat ---')
        pprint(flat)
        print('--- Hydrated ---')
        pprint(hydrated)

        return flat, hydrated, network

    def execute(self, actions):
        flat, hydrated, network = self.get_hypers(actions)

        hydrated['scope'] = 'hypersearch'

        env = OpenAIGym('CartPole-v0')
        agent = agents_dict[self.agent](
            states_spec=env.states,
            actions_spec=env.actions,
            network_spec=network,
            **hydrated
        )

        n_train, n_test = 250, 50
        runner = Runner(agent=agent, environment=env)
        runner.run(episodes=n_train, max_episode_timesteps=1000)  # train
        runner.run(episodes=n_test, max_episode_timesteps=1000, deterministic=True)  # test
        pdb.set_trace()
        # You may need to remove runner.py's close() calls so you have access to runner.episode_rewards, see
        # https://github.com/lefnire/tensorforce/commit/976405729abd7510d375d6aa49659f91e2d30a07

        # I personally save away the results so I can play with them manually w/ scikitlearn & SQL
        ep_results = runner.environment.episode_results
        reward = np.mean(ep_results['rewards'][-n_test:])
        # sql = "insert into runs (hypers, reward_avg) values (:hypers, :reward_avg)"
        # self.conn.execute(text(sql), hypers=json.dumps(flat), reward_avg=reward)
        print(flat, f"\nReward={reward}\n\n")

        runner.agent.close()
        runner.environment.close()

        return reward

    def get_winner(self):
        """After running this for a long time you can call this get_winner() separately to run a winning combo"""
        sql = "select id, hypers from runs where flag is null and agent=:agent order by reward_avg desc limit 1"
        winner = self.conn.execute(text(sql), agent=self.agent).fetchone()
        self.hardcoded = winner.hypers
        print(f'Using winner {winner.id}')
        return self.get_hypers({})


def main_gp():
    import gp
    from sklearn.feature_extraction import DictVectorizer

    # Encode features
    hsearch = HSearchEnv()
    actions, hypers, hardcoded = hsearch.actions, hsearch.hypers, hsearch.hardcoded
    hypers = {k: v for k, v in hypers.items() if k not in hardcoded}
    hsearch.close()

    # Build a matrix of features,  length = max feature size
    max_num_vals = max(actions.values(), key=lambda v: v.get('num_actions', 0))['num_actions']
    empty_obj = {k: None for k in hypers}
    mat = pd.DataFrame([empty_obj.copy() for _ in range(max_num_vals)])
    for k, hyper in hypers.items():
        for i, v in enumerate(hyper['vals']):
            # mat.loc[i,k] = v if hyper['type'] == 'bounded' else i
            mat.loc[i,k] = v
    mat.ffill(inplace=True)

    # Above is Pandas-friendly stuff, now convert to sklearn-friendly & pipe through OneHotEncoder
    vectorizer = DictVectorizer()
    vectorizer.fit(mat.T.to_dict().values())
    feat_names = vectorizer.get_feature_names()

    # Map TensorForce actions to GPyOpt-compatible `domain`
    # instantiate just to get actions (get them from hypers above?)
    bounds = []
    for k in feat_names:
        hyper = hypers.get(k, False)
        if hyper and hyper['type'] == 'bounded':
            b = [min(hyper['vals']), max(hyper['vals'])]
        else: b = [0, 1]
        bounds.append(b)

    # Specify the "loss" function (which we'll maximize) as a single rl_hsearch instantiate-and-run
    def loss_fn(params):
        hsearch = HSearchEnv()

        # Reverse the encoding
        # https://stackoverflow.com/questions/22548731/how-to-reverse-sklearn-onehotencoder-transform-to-recover-original-data
        # https://github.com/scikit-learn/scikit-learn/issues/4414
        reversed = vectorizer.inverse_transform([params])[0]
        actions = {}
        for k, v in reversed.items():
            if '=' not in k:
                actions[k] = v
                continue
            if k in actions: continue  # we already handled this x=y logic (below)
            # Find the winner (max) option for this key
            score, attr, val = v, k.split('=')[0], k.split('=')[1]
            for k2, score2 in reversed.items():
                if k2.startswith(attr + '=') and score2 > score:
                    score, val = score2, k2.split('=')[1]
            actions[attr] = val

        return hsearch.execute(actions)

    gp.bayesian_optimisation(
        n_iters=500,
        sample_loss=loss_fn,
        bounds=np.array(bounds),
    )


if __name__ == '__main__':
    main_gp()

import numpy as np
import torch
import torchquantum as tq

from torchpack.utils.logging import logger
from qiskit.providers.aer.noise import NoiseModel
from torchquantum.utils import get_provider


__all__ = [
    "NoiseModelTQ",
    "NoiseModelTQActivation",
    "NoiseModelTQPhase",
    "NoiseModelTQReadoutOnly",
    "NoiseModelTQActivationReadout",
    "NoiseModelTQPhaseReadout",
    "NoiseModelTQQErrorOnly",
]


def cos_adjust_noise(
    current_epoch,
    n_epochs,
    prob_schedule,
    prob_schedule_separator,
    orig_noise_total_prob,
):
    if prob_schedule is None:
        noise_total_prob = orig_noise_total_prob
    elif prob_schedule == "increase":
        # scale the cos
        if current_epoch <= prob_schedule_separator:
            noise_total_prob = orig_noise_total_prob * (
                -np.cos(current_epoch / prob_schedule_separator * np.pi) / 2 + 0.5
            )
        else:
            noise_total_prob = orig_noise_total_prob
    elif prob_schedule == "decrease":
        if current_epoch >= prob_schedule_separator:
            noise_total_prob = orig_noise_total_prob * (
                np.cos(
                    (current_epoch - prob_schedule_separator)
                    / (n_epochs - prob_schedule_separator)
                    * np.pi
                )
                / 2
                + 0.5
            )
        else:
            noise_total_prob = orig_noise_total_prob
    elif prob_schedule == "increase_decrease":
        # if current_epoch <= self.prob_schedule_separator:
        #     self.noise_total_prob = self.orig_noise_total_prob * \
        #         1 / (1 + np.exp(-(current_epoch - (
        #             self.prob_schedule_separator / 2)) / 10))
        # else:
        #     self.noise_total_prob = self.orig_noise_total_prob * \
        #         1 / (1 + np.exp((current_epoch - (
        #             self.n_epochs + self.prob_schedule_separator) / 2) /
        #                 10))
        if current_epoch <= prob_schedule_separator:
            noise_total_prob = orig_noise_total_prob * (
                -np.cos(current_epoch / prob_schedule_separator * np.pi) / 2 + 0.5
            )
        else:
            noise_total_prob = orig_noise_total_prob * (
                np.cos(
                    (current_epoch - prob_schedule_separator)
                    / (n_epochs - prob_schedule_separator)
                    * np.pi
                )
                / 2
                + 0.5
            )
    else:
        logger.warning(
            f"Not implemented schedule{prob_schedule}, " f"will not change prob!"
        )
        noise_total_prob = orig_noise_total_prob

    return noise_total_prob


def apply_readout_error_func(x, c2p_mapping, measure_info):
    # add readout error
    noise_free_0_probs = (x + 1) / 2
    noise_free_1_probs = 1 - (x + 1) / 2

    noisy_0_to_0_prob_all = []
    noisy_0_to_1_prob_all = []
    noisy_1_to_0_prob_all = []
    noisy_1_to_1_prob_all = []

    for k in range(x.shape[-1]):
        p_wire = [c2p_mapping[k]]
        noisy_0_to_0_prob_all.append(measure_info[tuple(p_wire)]["probabilities"][0][0])
        noisy_0_to_1_prob_all.append(measure_info[tuple(p_wire)]["probabilities"][0][1])
        noisy_1_to_0_prob_all.append(measure_info[tuple(p_wire)]["probabilities"][1][0])
        noisy_1_to_1_prob_all.append(measure_info[tuple(p_wire)]["probabilities"][1][1])

    noisy_0_to_0_prob_all = torch.tensor(noisy_0_to_0_prob_all, device=x.device)
    noisy_0_to_1_prob_all = torch.tensor(noisy_0_to_1_prob_all, device=x.device)
    noisy_1_to_0_prob_all = torch.tensor(noisy_1_to_0_prob_all, device=x.device)
    noisy_1_to_1_prob_all = torch.tensor(noisy_1_to_1_prob_all, device=x.device)

    noisy_measured_0 = (
        noise_free_0_probs * noisy_0_to_0_prob_all
        + noise_free_1_probs * noisy_1_to_0_prob_all
    )

    noisy_measured_1 = (
        noise_free_0_probs * noisy_0_to_1_prob_all
        + noise_free_1_probs * noisy_1_to_1_prob_all
    )
    noisy_expectation = noisy_measured_0 * 1 + noisy_measured_1 * (-1)

    return noisy_expectation


class NoiseCounter:
    '''count all the pauli error gate being sampled in self.sample_noise_op()
    '''
    def __init__(self):
        self.counter_x = 0
        self.counter_y = 0
        self.counter_z = 0
        self.counter_X = 0
        self.counter_Y = 0
        self.counter_Z = 0

    def add(self, error):
        if error == 'x':
            self.counter_x += 1
        elif error == 'y':
            self.counter_y += 1
        elif error == 'z':
            self.counter_z += 1
        if error == 'X':
            self.counter_X += 1
        elif error == 'Y':
            self.counter_Y += 1
        elif error == 'Z':
            self.counter_Z += 1
        else:
            pass
        
    def __str__(self) -> str:
        return f'single qubit error: pauli x = {self.counter_x}, pauli y = {self.counter_y}, pauli z = {self.counter_z}\n' + \
               f'double qubit error: pauli x = {self.counter_X}, pauli y = {self.counter_Y}, pauli z = {self.counter_Z}'



class NoiseModelTQ(object):
    """
    apply gate insertion and readout
    """

    def __init__(
        self,
        noise_model_name,
        n_epochs,
        noise_total_prob=None,
        ignored_ops=("id", "kraus", "reset"),
        prob_schedule=None,
        prob_schedule_separator=None,
        factor=None,
        add_thermal=True,
    ):
        self.noise_model_name = noise_model_name
        provider = get_provider(backend_name=noise_model_name)
        backend = provider.get_backend(noise_model_name)

        self.noise_model = NoiseModel.from_backend(
            backend, thermal_relaxation=add_thermal
        )
        self.noise_model_dict = self.noise_model.to_dict()
        self.is_add_noise = True
        self.v_c_reg_mapping = None
        self.p_c_reg_mapping = None
        self.p_v_reg_mapping = None
        self.orig_noise_total_prob = noise_total_prob
        self.noise_total_prob = noise_total_prob
        self.mode = "train"
        self.ignored_ops = ignored_ops

        self.parsed_dict = self.parse_noise_model_dict(self.noise_model_dict)
        self.parsed_dict = self.clean_parsed_noise_model_dict(self.parsed_dict, ignored_ops)
        self.n_epochs = n_epochs
        self.prob_schedule = prob_schedule
        self.prob_schedule_separator = prob_schedule_separator
        self.factor = factor
        self.noise_counter = NoiseCounter()

    def adjust_noise(self, current_epoch):
        self.noise_total_prob = cos_adjust_noise(
            current_epoch=current_epoch,
            n_epochs=self.n_epochs,
            prob_schedule=self.prob_schedule,
            prob_schedule_separator=self.prob_schedule_separator,
            orig_noise_total_prob=self.orig_noise_total_prob,
        )

    @staticmethod
    def clean_parsed_noise_model_dict(nm_dict, ignored_ops):
        # remove the ignored operation in the instructions and probs  
        # --> only get the pauli-x,y,z errors. ignore the thermal relaxation errors (kraus operator)

        def filter_inst(inst_list: list) -> list:
            new_inst_list = []
            for inst in inst_list:
                if inst['name'] in ignored_ops:
                    continue
                new_inst_list.append(inst)
            return new_inst_list

        ignored_ops           = set(ignored_ops)
        single_depolarization = set(['x', 'y', 'z'])
        double_depolarization = set(['IX', 'IY', 'IZ', 'XI', 'XX', 'XY', 'XZ', 'YI', 'YX', 'YY', 'YZ', 'ZI', 'ZX', 'ZY', 'ZZ']) # 16 - 1 = 15 combinations
        for operation, operation_info in nm_dict.items():
            for qubit, qubit_info in operation_info.items():
                inst_all = []
                prob_all = []
                if qubit_info["type"] == "qerror":
                    for inst, prob in zip(qubit_info["instructions"], qubit_info["probabilities"]):
                        if operation in ['x', 'sx', 'id', 'reset']:              # single qubit gate
                            if any([inst_one["name"] in single_depolarization for inst_one in inst]):
                                inst_all.append(filter_inst(inst))
                                prob_all.append(prob)
                        elif operation in ['cx']:                                # double qubit gate
                            try:
                                if inst[0]['params'][0] in double_depolarization and (inst[1]['name'] == 'id' or inst[2]['name'] == 'id'):
                                    inst_all.append(filter_inst(inst))
                                    prob_all.append(prob)
                            except:
                                pass  # don't know how to deal with this case
                        else:
                            raise Exception(f'{operation} not considered...')
                    nm_dict[operation][qubit]["instructions"] = inst_all
                    nm_dict[operation][qubit]["probabilities"] = prob_all
        return nm_dict

    @staticmethod
    def parse_noise_model_dict(nm_dict):
        # the qubits here are physical (p) qubits
        parsed = {}
        nm_list = nm_dict["errors"]

        for info in nm_list:
            val_dict = {
                "type": info["type"],
                "instructions": info.get("instructions", None),
                "probabilities": info["probabilities"],
            }

            if info["operations"][0] not in parsed.keys():
                parsed[info["operations"][0]] = {tuple(info["gate_qubits"][0]): val_dict}
            elif tuple(info["gate_qubits"][0]) not in parsed[info["operations"][0]].keys():
                parsed[info["operations"][0]][tuple(info["gate_qubits"][0])] = val_dict
            else:
                raise ValueError

        return parsed

    def magnify_probs(self, probs):
        if self.factor is not None:
            factor = self.factor
        else:
            if self.noise_total_prob is None:
                factor = 1
            else:
                factor = self.noise_total_prob / sum(probs)
        probs = [prob * factor for prob in probs]

        return probs

    def sample_noise_op(self, op_in):
        if not (self.mode == "train" and self.is_add_noise):
            return []

        op_name = op_in.name.lower()
        if op_name == "paulix":
            op_name = "x"
        elif op_name == "cnot":
            op_name = "cx"
        elif op_name in ["sx", "id"]:
            pass
        elif op_name == "rz":
            # no noise
            return []
        else:
            logger.warning(f"No noise model for {op_name} operation!")

        wires = op_in.wires

        p_wires = [self.p_v_reg_mapping["v2p"][wire] for wire in wires]

        if tuple(p_wires) in self.parsed_dict[op_name].keys():
            inst_prob = self.parsed_dict[op_name][tuple(p_wires)]
        else:
            # not in the real coupling map, so only give a dummy one
            if len(p_wires) == 1:
                inst_prob = self.parsed_dict[op_name][(0,)]
            elif len(p_wires) == 2:
                inst_prob = self.parsed_dict[op_name][(0, 1)]

        inst = inst_prob["instructions"]
        if len(inst) == 0:
            return []

        probs = inst_prob["probabilities"]

        magnified_probs = self.magnify_probs(probs)

        idx = np.random.choice(
            list(range(len(inst) + 1)), p=magnified_probs + [1 - sum(magnified_probs)]
        )
        if idx == len(inst):
            return []

        instructions = inst[idx]

        ops = []
        for instruction in instructions:
            v_wires = [self.p_v_reg_mapping["p2v"][qubit] for qubit in instruction["qubits"]]
            if instruction["name"] == "x":
                ops.append(tq.PauliX(wires=v_wires))
                self.noise_counter.add('x')
            elif instruction["name"] == "y":
                ops.append(tq.PauliY(wires=v_wires))
                self.noise_counter.add('y')
            elif instruction["name"] == "z":
                ops.append(tq.PauliZ(wires=v_wires))
                self.noise_counter.add('z')
            elif instruction["name"] == "reset":
                ops.append(tq.Reset(wires=v_wires))
            elif instruction["name"] == "pauli":
                twoqubit_depolarization = list(instruction['params'][0])  # ['XY'] --> ['X', 'Y']
                for singlequbit_deloparization, v_wire in zip(twoqubit_depolarization, v_wires):
                    if singlequbit_deloparization == 'X':
                        ops.append(tq.PauliX(wires=[v_wire]))
                        self.noise_counter.add('X')
                    elif singlequbit_deloparization == 'Y':
                        ops.append(tq.PauliY(wires=[v_wire]))
                        self.noise_counter.add('Y')
                    elif singlequbit_deloparization == 'Z':
                        ops.append(tq.PauliZ(wires=[v_wire]))
                        self.noise_counter.add('Z')
                    else:
                        pass  # 'I' case
            else:
                # ignore operations specified by self.ignored_ops
                # logger.warning(f"skip noise operation {instruction['name']}")
                continue

        return ops

    def apply_readout_error(self, x):
        c2p_mapping = self.p_c_reg_mapping["c2p"]
        measure_info = self.parsed_dict["measure"]

        return apply_readout_error_func(x, c2p_mapping, measure_info)


class NoiseModelTQActivation(object):
    """
    add noise to the activations
    """

    def __init__(
        self,
        mean=(0.0,),
        std=(1.0,),
        n_epochs=200,
        prob_schedule=None,
        prob_schedule_separator=None,
        after_norm=False,
        factor=None,
    ):
        self.mean = mean
        self.std = std
        self.is_add_noise = True
        self.mode = "train"
        self.after_norm = after_norm

        self.orig_std = std
        self.n_epochs = n_epochs
        self.prob_schedule = prob_schedule
        self.prob_schedule_separator = prob_schedule_separator
        self.factor = factor

    @property
    def noise_total_prob(self):
        return self.std

    @noise_total_prob.setter
    def noise_total_prob(self, value):
        self.std = value

    def adjust_noise(self, current_epoch):
        self.std = cos_adjust_noise(
            current_epoch=current_epoch,
            n_epochs=self.n_epochs,
            prob_schedule=self.prob_schedule,
            prob_schedule_separator=self.prob_schedule_separator,
            orig_noise_total_prob=self.orig_std,
        )

    def sample_noise_op(self, op_in):
        return []

    def apply_readout_error(self, x):
        return x

    def add_noise(self, x, node_id, is_after_norm=False):
        if (self.after_norm and is_after_norm) or (
            not self.after_norm and not is_after_norm
        ):
            if self.mode == "train" and self.is_add_noise:
                if self.factor is None:
                    factor = 1
                else:
                    factor = self.factor

                x = (
                    x
                    + torch.randn(x.shape, device=x.device) * self.std[node_id] * factor
                    + self.mean[node_id]
                )

        return x


class NoiseModelTQPhase(object):
    """
    add noise to rotation parameters
    """

    def __init__(
        self,
        mean=0.0,
        std=1.0,
        n_epochs=200,
        prob_schedule=None,
        prob_schedule_separator=None,
        factor=None,
    ):
        self.mean = mean
        self.std = std
        self.is_add_noise = True
        self.mode = "train"

        self.orig_std = std
        self.n_epochs = n_epochs
        self.prob_schedule = prob_schedule
        self.prob_schedule_separator = prob_schedule_separator
        self.factor = factor

    @property
    def noise_total_prob(self):
        return self.std

    @noise_total_prob.setter
    def noise_total_prob(self, value):
        self.std = value

    def adjust_noise(self, current_epoch):
        self.std = cos_adjust_noise(
            current_epoch=current_epoch,
            n_epochs=self.n_epochs,
            prob_schedule=self.prob_schedule,
            prob_schedule_separator=self.prob_schedule_separator,
            orig_noise_total_prob=self.orig_std,
        )

    def sample_noise_op(self, op_in):
        return []

    def apply_readout_error(self, x):
        return x

    def add_noise(self, phase):
        if self.mode == "train" and self.is_add_noise:
            if self.factor is None:
                factor = 1
            else:
                factor = self.factor
            phase = (
                phase
                + torch.randn(phase.shape, device=phase.device) * self.std * factor
                + self.mean
            )

        return phase


class NoiseModelTQReadoutOnly(NoiseModelTQ):
    def sample_noise_op(self, op_in):
        return []


class NoiseModelTQQErrorOnly(NoiseModelTQ):
    def apply_readout_error(self, x):
        return x


class NoiseModelTQActivationReadout(NoiseModelTQActivation):
    def __init__(
        self,
        noise_model_name,
        mean=0.0,
        std=1.0,
        n_epochs=200,
        prob_schedule=None,
        prob_schedule_separator=None,
        after_norm=False,
        factor=None,
    ):
        super().__init__(
            mean=mean,
            std=std,
            n_epochs=n_epochs,
            prob_schedule=prob_schedule,
            prob_schedule_separator=prob_schedule_separator,
            after_norm=after_norm,
            factor=factor,
        )
        provider = get_provider(backend_name=noise_model_name)
        backend = provider.get_backend(noise_model_name)

        self.noise_model = NoiseModel.from_backend(backend)
        self.noise_model_dict = self.noise_model.to_dict()
        self.is_add_noise = True
        self.v_c_reg_mapping = None
        self.p_c_reg_mapping = None
        self.p_v_reg_mapping = None

        self.parsed_dict = NoiseModelTQ.parse_noise_model_dict(self.noise_model_dict)

    def apply_readout_error(self, x):
        c2p_mapping = self.p_c_reg_mapping["c2p"]
        measure_info = self.parsed_dict["measure"]

        return apply_readout_error_func(x, c2p_mapping, measure_info)


class NoiseModelTQPhaseReadout(NoiseModelTQPhase):
    def __init__(
        self,
        noise_model_name,
        mean=0.0,
        std=1.0,
        n_epochs=200,
        prob_schedule=None,
        prob_schedule_separator=None,
        factor=None,
    ):
        super().__init__(
            mean=mean,
            std=std,
            n_epochs=n_epochs,
            prob_schedule=prob_schedule,
            prob_schedule_separator=prob_schedule_separator,
            factor=factor,
        )
        provider = get_provider(backend_name=noise_model_name)
        backend = provider.get_backend(noise_model_name)

        self.noise_model = NoiseModel.from_backend(backend)
        self.noise_model_dict = self.noise_model.to_dict()
        self.is_add_noise = True
        self.v_c_reg_mapping = None
        self.p_c_reg_mapping = None
        self.p_v_reg_mapping = None

        self.parsed_dict = NoiseModelTQ.parse_noise_model_dict(self.noise_model_dict)

    def apply_readout_error(self, x):
        c2p_mapping = self.p_c_reg_mapping["c2p"]
        measure_info = self.parsed_dict["measure"]

        return apply_readout_error_func(x, c2p_mapping, measure_info)

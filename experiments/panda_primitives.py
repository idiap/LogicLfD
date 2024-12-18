# -----------------------------------------------------------------------------
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of the LogicLfD project.
# Copyright (c) 2024 Idiap Research Institute <contact@idiap.ch>
# Contributor: Yan Zhang <yan.zhang@idiap.ch>
# -----------------------------------------------------------------------------

import time
import math
import numpy as np
from copy import deepcopy
from itertools import count
from termcolor import colored
from .utils import get_pose, set_pose, get_movable_joints, \
    set_joint_positions, add_fixed_constraint, enable_real_time, \
    disable_real_time, joint_controller, enable_gravity, \
    get_refine_fn, wait_for_duration, link_from_name, \
    get_body_name, sample_placement, end_effector_from_body,\
    approach_from_grasp, plan_joint_motion, GraspInfo, Pose, \
    INF, Point, inverse_kinematics, pairwise_collision, \
    remove_fixed_constraint, Attachment, get_sample_fn, \
    step_simulation, refine_path, plan_direct_joint_motion, \
    get_joint_positions, dump_world, wait_if_gui, flatten, \
    Euler, unit_pose, approximate_as_prism, point_from_pose, \
    multiply, stable_z, euler_from_quat, plan_joint_motion_interpolation, \
    inverse_kinematics_tracik, sample_placement_reachable

from pybullet_tools.ikfast.franka_panda.ik import is_ik_compiled, ikfast_inverse_kinematics
# TODO: deprecate
TOOL_POSE = Pose(euler=Euler(pitch=np.pi/2)) # l_gripper_tool_frame (+x out of gripper arm)

#####################################
# Box grasps

#GRASP_LENGTH = 0.04
GRASP_LENGTH = 0.
#GRASP_LENGTH = -0.01

#MAX_GRASP_WIDTH = 0.07
MAX_GRASP_WIDTH = np.inf

GRASP_INFO = {
    'top': GraspInfo(lambda body: get_top_grasps(body, under=True, tool_pose=Pose(), max_width=INF,  grasp_length=0),
                     approach_pose=Pose(0.1*Point(z=1))),
}

TOOL_FRAMES = {
    'panda': 'tool_link', # iiwa_link_ee | iiwa_link_ee_kuka
    'iiwa14': 'iiwa_link_ee_kuka',
}

DEBUG_FAILURE = False

##################################################

class BodyPose(object):
    num = count()
    def __init__(self, body, pose=None):
        if pose is None:
            pose = get_pose(body)
        self.body = body
        self.pose = pose
        self.index = next(self.num)
    @property
    def value(self):
        return self.pose
    def assign(self):
        set_pose(self.body, self.pose)
        return self.pose
    def __repr__(self):
        index = self.index
        #index = id(self) % 1000
        return 'p{}'.format(index)


class BodyGrasp(object):
    num = count()
    def __init__(self, body, grasp_pose, approach_pose, robot, link):
        self.body = body
        self.grasp_pose = grasp_pose
        self.approach_pose = approach_pose
        self.robot = robot
        self.link = link
        self.index = next(self.num)
    @property
    def value(self):
        return self.grasp_pose
    @property
    def approach(self):
        return self.approach_pose
    #def constraint(self):
    #    grasp_constraint()
    def attachment(self):
        return Attachment(self.robot, self.link, self.grasp_pose, self.body)
    def assign(self):
        return self.attachment().assign()
    def __repr__(self):
        index = self.index
        #index = id(self) % 1000
        return 'g{}'.format(index)

class BodyConf(object):
    num = count()
    def __init__(self, body, configuration=None, joints=None):
        if joints is None:
            joints = get_movable_joints(body)
        if configuration is None:
            configuration = get_joint_positions(body, joints)
        self.body = body
        self.joints = joints
        self.configuration = configuration
        self.index = next(self.num)
    @property
    def values(self):
        return self.configuration
    def assign(self):
        set_joint_positions(self.body, self.joints, self.configuration)
        return self.configuration
    def __repr__(self):
        index = self.index
        #index = id(self) % 1000
        return 'q{}'.format(index)

class BodyPath(object):
    def __init__(self, body, path, joints=None, attachments=[]):
        if joints is None:
            joints = get_movable_joints(body)
        self.body = body
        self.path = path
        self.joints = joints
        self.attachments = attachments
    def bodies(self):
        return set([self.body] + [attachment.body for attachment in self.attachments])
    def iterator(self):
        # TODO: compute and cache these
        # TODO: compute bounding boxes as well
        for i, configuration in enumerate(self.path):
            set_joint_positions(self.body, self.joints, configuration)
            for grasp in self.attachments:
                grasp.assign()
            yield i
    def control(self, real_time=False, dt=0):
        # TODO: just waypoints
        if real_time:
            enable_real_time()
        else:
            disable_real_time()
        for values in self.path:
            for _ in joint_controller(self.body, self.joints, values):
                enable_gravity()
                if not real_time:
                    step_simulation()
                time.sleep(dt)
            # add grasp and ungrasp objects
            for grasp in self.attachments:
                grasp.assign()
    # def full_path(self, q0=None):
    #     # TODO: could produce sequence of savers
    def refine(self, num_steps=0):
        return self.__class__(self.body, refine_path(self.body, self.joints, self.path, num_steps), self.joints, self.attachments)
    def reverse(self):
        return self.__class__(self.body, self.path[::-1], self.joints, self.attachments)
    def __repr__(self):
        return '{}({},{},{},{})'.format(self.__class__.__name__, self.body, len(self.joints), len(self.path), len(self.attachments))

##################################################

class ApplyForce(object):
    def __init__(self, body, robot, link):
        self.body = body
        self.robot = robot
        self.link = link
    def bodies(self):
        return {self.body, self.robot}
    def iterator(self, **kwargs):
        return []
    def refine(self, **kwargs):
        return self
    def __repr__(self):
        return '{}({},{})'.format(self.__class__.__name__, self.robot, self.body)

class Attach(ApplyForce):
    def control(self, **kwargs):
        # TODO: store the constraint_id?
        add_fixed_constraint(self.body, self.robot, self.link)
    def reverse(self):
        return Detach(self.body, self.robot, self.link)

class Detach(ApplyForce):
    def control(self, **kwargs):
        remove_fixed_constraint(self.body, self.robot, self.link)
    def reverse(self):
        return Attach(self.body, self.robot, self.link)

class Command(object):
    num = count()
    def __init__(self, body_paths):
        self.body_paths = body_paths
        self.index = next(self.num)
    def bodies(self):
        return set(flatten(path.bodies() for path in self.body_paths))
    # def full_path(self, q0=None):
    #     if q0 is None:
    #         q0 = Conf(self.tree)
    #     new_path = [q0]
    #     for partial_path in self.body_paths:
    #         new_path += partial_path.full_path(new_path[-1])[1:]
    #     return new_path
    def step(self):
        for i, body_path in enumerate(self.body_paths):
            for j in body_path.iterator():
                msg = '{},{}) step?'.format(i, j)
                wait_if_gui(msg)
                #print(msg)
    def execute(self, time_step=0.05):
        for i, body_path in enumerate(self.body_paths):
            for j in body_path.iterator():
                #time.sleep(time_step)
                wait_for_duration(time_step)
    def control(self, real_time=False, dt=0): # TODO: real_time
        for body_path in self.body_paths:
            body_path.control(real_time=real_time, dt=dt)
    def refine(self, **kwargs):
        return self.__class__([body_path.refine(**kwargs) for body_path in self.body_paths])
    def reverse(self):
        return self.__class__([body_path.reverse() for body_path in reversed(self.body_paths)])
    def __repr__(self):
        index = self.index
        #index = id(self) % 1000
        return 'c{}'.format(index)

#######################################################

def get_tool_link(robot):
    return link_from_name(robot, TOOL_FRAMES[get_body_name(robot)])
    

def get_top_grasps(body, under=False, tool_pose=TOOL_POSE, body_pose=unit_pose(),
                   max_width=MAX_GRASP_WIDTH, grasp_length=GRASP_LENGTH):
    # TODO: rename the box grasps
    center, (w, l, h) = approximate_as_prism(body, body_pose=body_pose)
    reflect_z = Pose(euler=[0, math.pi, 0])
    translate_z = Pose(point=[0, 0, h / 2 - grasp_length])
    translate_center = Pose(point=point_from_pose(body_pose)-center)
    grasps = []
    if w <= max_width:
        for i in range(1 + under):
            rotate_z = Pose(euler=[0, 0, math.pi / 2 + i * math.pi])
            grasps += [multiply(tool_pose, translate_z, rotate_z,
                                reflect_z, translate_center, body_pose)]
    if l <= max_width:
        for i in range(1 + under):
            rotate_z = Pose(euler=[0, 0, i * math.pi])
            grasps += [multiply(tool_pose, translate_z, rotate_z,
                                reflect_z, translate_center, body_pose)]
    return grasps

def get_grasp_gen(robot, grasp_name='top', verbose=False):
    if verbose:
        print(colored('\n Running get_grasp_gen function \n', 'red'))
    grasp_info = GRASP_INFO[grasp_name]
    tool_link = get_tool_link(robot)
    def gen(body, verbose=verbose):
        grasp_poses = grasp_info.get_grasps(body)
        # TODO: continuous set of grasps
        for grasp_pose in grasp_poses:
            body_grasp = BodyGrasp(body, grasp_pose, grasp_info.approach_pose, robot, tool_link)
            if verbose:
                print(colored(f'\nGrasp: {body_grasp.value} \n', 'green'))
            yield (body_grasp,)
    return gen


def get_stable_gen(fixed=[], verbose=False, reach_range=(0.25, 0.5),
                   reach_theta=(-np.pi*0.75, np.pi*0.75)):
    if verbose:
        print(colored('\n Running get_stable_gen function \n', 'red'))
    def gen(body, surface, verbose=verbose):
        while True:
            pose = sample_placement_reachable(body, surface, 
                                              reach_range=reach_range, 
                                              reach_theta=reach_theta)
            body_pose = BodyPose(body, pose)
            if verbose:
                print(colored(f'\n Sampled Pose {body_pose.value} \n', 'green'))    
            yield (body_pose,)
    return gen

# def get_stable_gen2(fixed=[], verbose=False, reach_range=(0.25, 0.5),
#                    reach_theta=(-np.pi*0.75, np.pi*0.75)):
#     if verbose:
#         print(colored('\n Running get_stable_gen function \n', 'red'))
#     def gen(body, surface, verbose=verbose):
#         while True:
#             pose = sample_placement_reachable2(body, surface, 
#                                               reach_range=reach_range, 
#                                               reach_theta=reach_theta)
#             body_pose = BodyPose(body, pose)
#             if verbose:
#                 print(colored(f'\n Sampled Pose {body_pose.value} \n', 'green'))    
#             yield (body_pose,)
#     return gen

def get_stack_gen(verbose=False):
    if verbose:
        print(colored('\n Running get_stack_gen function \n', 'red'))
    def gen(body1, body2, pose, verbose=verbose):
        point = Point(x=pose.value[0][0], y=pose.value[0][1], 
              z=stable_z(body1, body2))
        roll, pitch, yaw = euler_from_quat(pose.value[1])
        euler = Euler(roll=roll, pitch=pitch, yaw=yaw)
        # set_pose(body1, Pose(point=point, euler=euler))
        body_pose = BodyPose(body1, 
                             Pose(point=point, euler=euler))
        if verbose:
            print(colored(f'\n Stack Pose {body_pose.value} \n', 'green'))
        yield (body_pose,)
    return gen

# ==================================================
def get_ik_fn(robot, fixed=[], teleport=False, num_attempts=10):
    movable_joints = get_movable_joints(robot)
    sample_fn = get_sample_fn(robot, movable_joints)
    def fn(body, pose, grasp):
        obstacles = [body] + fixed
        gripper_pose = end_effector_from_body(pose.pose, grasp.grasp_pose)
        approach_pose = approach_from_grasp(grasp.approach_pose, gripper_pose)
        for _ in range(num_attempts):
            set_joint_positions(robot, movable_joints, sample_fn()) # Random seed
            # TODO: multiple attempts?
            q_approach = inverse_kinematics(robot, grasp.link, approach_pose)
            if (q_approach is None) or any(pairwise_collision(robot, b) for b in obstacles):
                continue
            conf = BodyConf(robot, q_approach)
            q_grasp = inverse_kinematics(robot, grasp.link, gripper_pose)
            if (q_grasp is None) or any(pairwise_collision(robot, b) for b in obstacles):
                continue
            if teleport:
                path = [q_approach, q_grasp]
            else:
                conf.assign()
                #direction, _ = grasp.approach_pose
                #path = workspace_trajectory(robot, grasp.link, point_from_pose(approach_pose), -direction,
                #                                   quat_from_pose(approach_pose))
                path = plan_direct_joint_motion(robot, conf.joints, q_grasp, obstacles=obstacles)
                if path is None:
                    if DEBUG_FAILURE: wait_if_gui('Approach motion failed')
                    continue
            command = Command([BodyPath(robot, path),
                               Attach(body, robot, grasp.link),
                               BodyPath(robot, path[::-1], attachments=[grasp])])
            return (conf, command)
            # TODO: holding collisions
        return None
    return fn

def get_robots_ik_fn(fixed=[], teleport=False, num_attempts=10, verbose=False):
    # TODO this ik function does not work for the panda robot
    if verbose:
        print(colored('\n Running get_robots_ik_fn function \n', 'red'))
    def fn(robot, body, pose, grasp, verbose=verbose):
        movable_joints = get_movable_joints(robot)
        sample_fn = get_sample_fn(robot, movable_joints)
        obstacles = [body] + fixed # not collide with body
        # grasp.grasp pose indicates body's pose in gripper frame
        gripper_pose = end_effector_from_body(pose.pose, grasp.grasp_pose)
        approach_pose = approach_from_grasp(grasp.approach_pose, gripper_pose)
       
        for _ in range(num_attempts):
            # set_joint_positions(robot, movable_joints, sample_fn()) # Random seed
            # TODO: multiple attempts?
            q_approach = inverse_kinematics_tracik(robot, grasp.link, approach_pose)
            # if (q_approach is None) or any(pairwise_collision(robot, b) for b in obstacles):
            #     continue
            if q_approach is None:
                continue
            conf_approach = BodyConf(robot, q_approach)
            q_grasp = inverse_kinematics_tracik(robot, grasp.link, gripper_pose)
            # if (q_grasp is None) or any(pairwise_collision(robot, b) for b in obstacles):
            #     continue
            if q_grasp is None:
                continue
            conf_grasp = BodyConf(robot, q_grasp)
            # if teleport:
            #     path = [q_approach, q_grasp]
            # else:
            #     conf.assign()
            #     #direction, _ = grasp.approach_pose
            #     #path = workspace_trajectory(robot, grasp.link, point_from_pose(approach_pose), -direction,
            #     #                                   quat_from_pose(approach_pose))
            #     path = plan_direct_joint_motion(robot, conf.joints, q_grasp, obstacles=obstacles)
            #     if path is None:
            #         if DEBUG_FAILURE: wait_if_gui('Approach motion failed')
            #         continue
            # command = Command([BodyPath(robot, path),
            #                    Attach(body, robot, grasp.link),
            #                    BodyPath(robot, path[::-1], attachments=[grasp])])
            if conf_approach is None or conf_grasp is None:
                if DEBUG_FAILURE: wait_if_gui('Approach motion failed')
                continue
            else:
                if verbose:
                    print(colored(f'\n Robot approach conf: {conf_approach} \n', 'green'))
                    print(colored(f'\n Robot grasp conf: {conf_grasp} \n', 'green'))
                return (conf_approach, conf_grasp)
        return None
    return fn
##################################################

def assign_fluent_state(fluents):
    obstacles = []
    grasp = None
    for fluent in fluents:
        name, args = fluent[0], fluent[1:]
        if name == 'atpose':
            o, p = args
            obstacles.append(o)
            p.assign()
        elif name == 'athandpose':
            o, g = args[1], args[2]
            grasp = g
            g.assign()
        else:
            # raise ValueError(name)
            continue
    return obstacles, grasp

def get_free_motion_gen(robot, fixed=[], verbose=False,
                        teleport=False, self_collisions=True):
    def fn(conf1, conf2, fluents=[], verbose=verbose):
        assert ((conf1.body == conf2.body) and (conf1.joints == conf2.joints))
        if teleport:
            path = [conf1.configuration, conf2.configuration]
        else:
            conf1.assign()
            obstacles, _ = assign_fluent_state(fluents)
            obstacles += fixed
            path = plan_joint_motion(robot, conf2.joints, conf2.configuration, obstacles=obstacles, self_collisions=self_collisions)
            if path is None:
                if DEBUG_FAILURE: wait_if_gui('Free motion failed')
                return None
        command = Command([BodyPath(robot, path, joints=conf2.joints)])
        if verbose:
            print(colored(f'\n Free motion command: {len(path)} \n', 'green'))
        return (command,)
    return fn


def get_holding_motion_gen(robot, fixed=[], verbose=False,
                           teleport=False, self_collisions=True):
    def fn(conf1, conf2, body, grasp, fluents=[], verbose=verbose):
        assert ((conf1.body == conf2.body) and (conf1.joints == conf2.joints))
        if teleport:
            path = [conf1.configuration, conf2.configuration]
        else:
            conf1.assign()
            obstacles, _ = assign_fluent_state(fluents)
            obstacles += fixed
            path = plan_joint_motion(robot, conf2.joints, conf2.configuration,
                                     obstacles=obstacles, attachments=[grasp.attachment()], self_collisions=self_collisions)
            if path is None:
                if DEBUG_FAILURE: wait_if_gui('Holding motion failed')
                return None
        command = Command([BodyPath(robot, path, joints=conf2.joints, attachments=[grasp])])
        if verbose:
            print(colored(f'\n Holding motion command: {len(path)} \n', 'green'))
        return (command,)
    return fn

def get_motion_gen(fixed=[], verbose=False, 
                   teleport=False, self_collisions=True):
    if verbose:
        print(colored('\n Running get_motion_gen function \n', 'red'))
    def fn(robot, conf1, conf2, fluents=[], verbose=verbose):
        assert ((conf1.body == conf2.body) and (conf1.joints == conf2.joints))
        if teleport:
            path = [conf1.configuration, conf2.configuration]
        else:
            grasp = None
            conf1.assign()
            
            obstacles, grasp = assign_fluent_state(fluents)
            obstacles = fixed + obstacles
            if grasp is None:
                path = plan_joint_motion_interpolation(robot, conf2.joints, conf2.configuration, 
                                         obstacles=obstacles, self_collisions=self_collisions)
            else:
                path = plan_joint_motion_interpolation(robot, conf2.joints, conf2.configuration,
                                        obstacles=obstacles, attachments=[grasp.attachment()], 
                                        self_collisions=self_collisions)
            if path is None:
                if DEBUG_FAILURE: wait_if_gui('Holding motion failed')
                return None
        if grasp is None:
            command = Command([BodyPath(robot, path, joints=conf2.joints)])
        else:
            command = Command([BodyPath(robot, path, joints=conf2.joints, attachments=[grasp])])
        if verbose:
            print(colored(f'\n Command found \n', 'green'))
        return (command,)
    return fn

##################################################

def get_movable_collision_test():
    def test(command, body, pose):
        if body in command.bodies():
            return False
        pose.assign()
        for path in command.body_paths:
            moving = path.bodies()
            if body in moving:
                # TODO: cannot collide with itself
                continue
            for _ in path.iterator():
                # TODO: could shuffle this
                if any(pairwise_collision(mov, body) for mov in moving):
                    if DEBUG_FAILURE: wait_if_gui('Movable collision')
                    return True
        return False
    return test

def get_check_colfree_block():
    # TODO
    raise NotImplementedError()
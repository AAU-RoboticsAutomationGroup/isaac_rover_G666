
import math
from cmath import inf
import numpy as np
import os
import torch
import xml.etree.ElementTree as ET

from utils.torch_jit_utils import *
from tasks.base.vec_task import VecTask
#from utils.kinematics import Rover

from isaacgym import gymutil, gymtorch, gymapi
import open3d as o3d


########### Error received #######
## ImportError: PyTorch was imported before isaacgym modules.  Please import torch after isaacgym modules.
# Cannot implement a artificial camera in exomy version scripts. 
# However, this specific script has worked before, so the implementation of a camera in line 265 is possible.


class Exomy(VecTask):

    def __init__(self, cfg, sim_device, graphics_device_id, headless):

        self.cfg = cfg
        #self.Kinematics = Rover()
        self.max_episode_length = self.cfg["env"]["maxEpisodeLength"]
        self.cfg["env"]["numObservations"] = 13
        self.cfg["env"]["numActions"] = 12
        self.max_effort_vel = math.pi
        self.max_effort_pos = math.pi/2
        super().__init__(config=self.cfg, sim_device=sim_device, graphics_device_id=graphics_device_id, headless=headless)
        # Retrieves buffer for Actor root states.
        # position([0:3]), rotation([3:7]), linear velocity([7:10]), and angular velocity([10:13])
        # Buffer has shape (num_environments, num_actors * 13).
        dofs_per_env = 17

        self.root_tensor = self.gym.acquire_actor_root_state_tensor(self.sim)
        self.dof_state_tensor = self.gym.acquire_dof_state_tensor(self.sim)
        
        # Convert buffer to vector, one is created for the robot and for the marker.
        vec_root_tensor = gymtorch.wrap_tensor(self.root_tensor).view(self.num_envs, 2, 13)
        vec_dof_tensor = gymtorch.wrap_tensor(self.dof_state_tensor).view(self.num_envs, dofs_per_env, 2)
        #print(vec_dof_tensor)
        # Position vector for robot
        self.root_states = vec_root_tensor[:, 0, :]
        self.root_positions = self.root_states[:, 0:3]
        # Rotation of robot
        self.root_quats = self.root_states[:, 3:7]
        # Linear Velocity of robot
        self.root_linvels = self.root_states[:, 7:10]
        # Angular Velocity of robot
        self.root_angvels = self.root_states[:, 10:13]


        self.target_root_positions = torch.zeros((self.num_envs, 3), device=self.device, dtype=torch.float32)
        self.target_root_positions[:, 2] = 0

        #print(self.target_root_positions)

        # Marker position
        self.marker_states = vec_root_tensor[:, 1, :]
        self.marker_positions = self.marker_states[:, 0:3]


        # self.dof_states = vec_dof_tensor
        # self.dof_positions = vec_dof_tensor[..., 0]
        # self.dof_velocities = vec_dof_tensor[..., 1]
        
        dof_state_tensor = self.gym.acquire_dof_state_tensor(self.sim)
        self.dof_states = gymtorch.wrap_tensor(dof_state_tensor)
        self.dof_positions = self.dof_states.view(self.num_envs, self.num_dof, 2)[..., 0]
        self.dof_velocities = self.dof_states.view(self.num_envs, self.num_dof, 2)[..., 1]


        self.gym.refresh_actor_root_state_tensor(self.sim)
        self.gym.refresh_dof_state_tensor(self.sim)

        self.initial_root_states = self.root_states.clone()
        self.initial_dof_states = self.dof_states.clone()

        
        # Control tensor
        self.all_actor_indices = torch.arange(self.num_envs * 2, dtype=torch.int32, device=self.device).reshape((self.num_envs, 2))

        
        cam_pos = gymapi.Vec3(-1.0, -0.6, 0.8)
        cam_target = gymapi.Vec3(1.0, 1.0, 0.15)
        self.gym.viewer_camera_look_at(self.viewer, None, cam_pos, cam_target)

        self.frame_count = 0 #used for pointcloud

    def create_sim(self):
        # implement sim set up and environment creation here
        #    - set up-axis
        self.sim_params.up_axis = gymapi.UP_AXIS_Z
        
        
        #    - set up gravity
        self.sim_params.gravity.x = 0
        self.sim_params.gravity.y = 0
        self.sim_params.gravity.z = -3.721  
        #    - call super().create_sim with device args (see docstring)
        self.sim = super().create_sim(self.device_id, self.graphics_device_id, self.physics_engine, self.sim_params)

        #    - set time step length
        self.dt = self.sim_params.dt
        #    - setup asset
        self._create_exomy_asset()
        #    - create ground plane
        self._create_ground_plane()
        #    - set up environments
        self._create_envs(self.num_envs, self.cfg["env"]['envSpacing'], int(np.sqrt(self.num_envs)))

        





    def _create_exomy_asset(self):
        pass


    def _create_ground_plane(self):
        plane_params = gymapi.PlaneParams()
        # set the nroaml force to be z dimension
        plane_params.normal = gymapi.Vec3(0.0,0.0,1.0)
        self.gym.add_ground(self.sim, plane_params)
    
    def set_targets(self, env_ids):
        num_sets = len(env_ids)
        # set target position randomly with x, y in (-2, 2) and z in (1, 2)
        #print("ASDO:JNHSAOJPNHDJNO:HASDJUOIP")
        self.target_root_positions[env_ids, 0:2] = (torch.rand(num_sets, 2, device=self.device) * 7) - 3.5
        self.target_root_positions[env_ids, 2] = 0
        self.marker_positions[env_ids] = self.target_root_positions[env_ids]
        # copter "position" is at the bottom of the legs, so shift the target up so it visually aligns better
        #self.marker_positions[env_ids, 2] += 0.4
        actor_indices = self.all_actor_indices[env_ids, 1].flatten()
        self.gym.set_actor_root_state_tensor_indexed(self.sim,self.root_tensor, gymtorch.unwrap_tensor(actor_indices), num_sets)

        return actor_indices


    def _create_envs(self,num_envs,spacing, num_per_row):
       # define plane on which environments are initialized
        lower = gymapi.Vec3(0.5 * -spacing, -spacing, 0.0)
        upper = gymapi.Vec3(0.5 * spacing, spacing, spacing)

        asset_root = "../assets"
        exomy_asset_file = "urdf/exomy_model/urdf/exomy_model.urdf"
        
        # if "asset" in self.cfg["env"]:
        #     asset_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), self.cfg["env"]["asset"].get("assetRoot", asset_root))
        #     asset_file = self.cfg["env"]["asset"].get("assetFileName", asset_file)

        # asset_path = os.path.join(asset_root, asset_file)
        # asset_root = os.path.dirname(asset_path)
        # asset_file = os.path.basename(asset_path)

        asset_options = gymapi.AssetOptions()
        asset_options.fix_base_link = False
        asset_options.disable_gravity = False
        asset_options.armature = 0.01
        # use default convex decomposition params
        asset_options.vhacd_enabled = False

        print("Loading asset '%s' from '%s'" % (exomy_asset_file, asset_root))
        exomy_asset = self.gym.load_asset(self.sim, asset_root, exomy_asset_file, asset_options)
        self.num_dof = self.gym.get_asset_dof_count(exomy_asset)
        #print(self.num_dof)
        #################################################
        # get joint limits and ranges for Franka
        exomy_dof_props = self.gym.get_asset_dof_properties(exomy_asset)
        exomy_lower_limits = exomy_dof_props["lower"]
        exomy_upper_limits = exomy_dof_props["upper"]
        exomy_ranges = exomy_upper_limits - exomy_lower_limits
        exomy_mids = 0.5 * (exomy_upper_limits + exomy_lower_limits)
        exomy_num_dofs = len(exomy_dof_props)

        #################################################
        # set default DOF states
        default_dof_state = np.zeros(exomy_num_dofs, gymapi.DofState.dtype)
        default_dof_state["pos"] = exomy_mids


        
        exomy_dof_props["driveMode"] = [
            gymapi.DOF_MODE_VEL, #0  #LFB bogie
            gymapi.DOF_MODE_POS,  #1  #LF POS
            gymapi.DOF_MODE_VEL,  #2  #LF DRIVE
            gymapi.DOF_MODE_POS,  #3  #LM POS
            gymapi.DOF_MODE_VEL,  #4  #LM DRIVE
            gymapi.DOF_MODE_VEL, #5  #MRB bogie
            gymapi.DOF_MODE_POS,  #6  #LR POS
            gymapi.DOF_MODE_VEL,  #7  #LR DRIVE
            gymapi.DOF_MODE_POS,  #8  #RR POS
            gymapi.DOF_MODE_VEL,  #9  #RR DRIVE
            gymapi.DOF_MODE_VEL, #10 #RFB bogie
            gymapi.DOF_MODE_POS,  #11 #RF POS 
            gymapi.DOF_MODE_VEL,  #12 #RF DRIVE
            gymapi.DOF_MODE_POS,  #13 #RM POS
            gymapi.DOF_MODE_VEL,  #14 #RM DRIVE
            gymapi.DOF_MODE_VEL,  #15 #SHIT EYE 1
            gymapi.DOF_MODE_VEL   #16 #SHIT EYE 2
        ]

        

        exomy_dof_props["stiffness"].fill(800.0)
        exomy_dof_props["damping"].fill(0.01)
        exomy_dof_props["friction"].fill(0.5)
        pose = gymapi.Transform()
        pose.p.z = 0.2
        # asset is rotated z-up by default, no additional rotations needed
        pose.r = gymapi.Quat(0.0, 0.0, 1.0, 0.0)

        self.exomy_handles = []
        self.envs = []

        self.camera_handles = []
        self.cameraheight = 240
        self.camerawidth = 424
        
        #################################################
        #Create marker
        default_pose = gymapi.Transform()
        default_pose.p.z = 0.0
        default_pose.p.x = 0.1        
        marker_options = gymapi.AssetOptions()
        marker_options.fix_base_link = True
        marker_asset = self.gym.create_sphere(self.sim, 0.1, marker_options)
        for i in range(num_envs):
            # Create environment
            env0 = self.gym.create_env(self.sim, lower, upper, num_per_row)
            self.envs.append(env0)

            
            exomy0_handle = self.gym.create_actor(
                env0,  # Environment Handle
                exomy_asset,  # Asset Handle
                pose,  # Transform of where the actor will be initially placed
                "exomy",  # Name of the actor
                i,  # Collision group that actor will be part of
                1,  # Bitwise filter for elements in the same collisionGroup to mask off collision
            )
            self.exomy_handles.append(exomy0_handle)

            
    
            # Configure DOF properties
            # Set initial DOF states
            # gym.set_actor_dof_states(env0, exomy0_handle, default_dof_state, gymapi.STATE_ALL)
            # Set DOF control properties
            self.gym.set_actor_dof_properties(env0, exomy0_handle, exomy_dof_props)
            #print(self.gym.get_actor_dof_properties((env0, exomy0_handle))

            # Spawn marker
            marker_handle = self.gym.create_actor(env0, marker_asset, default_pose, "marker", i, 1, 1)
            self.gym.set_rigid_body_color(env0, marker_handle, 0, gymapi.MESH_VISUAL_AND_COLLISION, gymapi.Vec3(1, 0, 0))
        
        #################################################
        # Create camera
        camera_props = gymapi.CameraProperties()
        camera_props.width = self.camerawidth
        camera_props.height = self.cameraheight
        camera_props.near_plane = 0.16
        camera_props.far_plane = 3
        camera_handle = self.gym.create_camera_sensor(env0, camera_props)
        self.camera_handles.append(camera_handle)
            # Attatch camera to body
        body_handle = self.gym.get_actor_rigid_body_handle(env0, exomy0_handle, 18)
        local_transform = gymapi.Transform()
        local_transform.p = gymapi.Vec3(0,0,0.01) #Units in meters
                                                        # tilt,             yaw,                pan
        local_transform.r = gymapi.Quat.from_euler_zyx(np.radians(180.0), np.radians(-90.0), np.radians(0.0))
        self.gym.attach_camera_to_body(camera_handle, env0, body_handle, local_transform, gymapi.FOLLOW_TRANSFORM)

    def reset_idx(self, env_ids):
        # set rotor speeds
        
        num_resets = len(env_ids)

        target_actor_indices = self.set_targets(env_ids)

        actor_indices = self.all_actor_indices[env_ids, 0].flatten()
        #print(self.root_states[0])
        self.root_states[env_ids] = self.initial_root_states[env_ids]
        self.root_states[env_ids, 0] = torch_rand_float(-1.5, 1.5, (num_resets, 1), self.device).flatten()
        self.root_states[env_ids, 1] = torch_rand_float(-1.5, 1.5, (num_resets, 1), self.device).flatten()
        self.root_states[env_ids, 2] = 0.1#torch_rand_float(-0.2, 1.5, (num_resets, 1), self.device).flatten()
        self.dof_states = self.initial_dof_states
        self.gym.set_actor_root_state_tensor_indexed(self.sim,self.root_tensor, gymtorch.unwrap_tensor(actor_indices), num_resets)
        #print(self.root_states[0])
        self.dof_positions = 0
        self.gym.set_dof_state_tensor_indexed(self.sim, gymtorch.unwrap_tensor(self.dof_states), gymtorch.unwrap_tensor(actor_indices), num_resets)

        self.reset_buf[env_ids] = 0
        self.progress_buf[env_ids] = 0

        return torch.unique(torch.cat([target_actor_indices, actor_indices]))

        #Used to reset a single environment
        



    def pre_physics_step(self, actions):
        # 
        set_target_ids = (self.progress_buf % 1000 == 0).nonzero(as_tuple=False).squeeze(-1)
        if  torch.any(self.progress_buf % 1000 == 0):
            print(self.marker_positions)
        target_actor_indices = torch.tensor([], device=self.device, dtype=torch.int32)
        #if len(set_target_ids) > 0:
            #target_actor_indices = self.set_targets(set_target_ids)
    
        reset_env_ids = self.reset_buf.nonzero(as_tuple=False).squeeze(-1)
        #print(self.reset_buf)
        actor_indices = torch.tensor([], device=self.device, dtype=torch.int32)
        #print(reset_env_ids.size())
        if len(reset_env_ids) > 0:
            actor_indices = self.reset_idx(reset_env_ids)


        reset_indices = torch.unique(torch.cat([target_actor_indices]))
        if len(reset_indices) > 0:
            self.gym.set_actor_root_state_tensor_indexed(self.sim, self.root_tensor, gymtorch.unwrap_tensor(reset_indices), len(reset_indices))
        # if  (self.progress_buf % 500 == 0).nonzero(as_tuple=False).squeeze(-1):
        #     print(self.marker_positions)
        #print(self.marker_positions)
        #print("exomy")
        #print(self.target_root_positions)
        actions_tensor = torch.zeros(self.num_envs * self.num_dof, device=self.device, dtype=torch.float)
        _actions = actions.to(self.device)

        # actions_tensor[::self.num_dof] = actions.to(self.device).squeeze()
        # #print(np.shape(_actions))
        #print(np.shape(_actions))
        # print(actions.size())

        DRV_LF_joint_dof_handle = self.gym.find_actor_dof_handle(self.envs[0], self.exomy_handles[0], "DRV_LF_joint")
        #max = 100
        max = 2
        #actions_tensor = actions.to(self.device).squeeze() * 400
        #pos, vel = self.Kinematics.Get_AckermannValues(1,1)
        actions_tensor[1::17]=(_actions[:,0]-0.5) * 2 * self.max_effort_pos  #1  #LF POS
        actions_tensor[2::17]=(_actions[:,1]-0.5) * 2 * self.max_effort_vel #2  #LF DRIVE
        actions_tensor[3::17]=(_actions[:,2]-0.5) * 2 * self.max_effort_pos #3  #LM POS
        actions_tensor[4::17]=(_actions[:,3]-0.5) * 2 * self.max_effort_vel #4  #LM DRIVE
        actions_tensor[6::17]=(_actions[:,4]-0.5) * 2 * self.max_effort_pos #6  #LR POS
        actions_tensor[7::17]=(_actions[:,5]-0.5) * 2 * self.max_effort_vel #7  #LR DRIVE
        actions_tensor[8::17]=(_actions[:,6]-0.5) * 2 * self.max_effort_pos #8  #RR POS
        actions_tensor[9::17]=(_actions[:,7]-0.5) * 2 * self.max_effort_vel #9  #RR DRIVE
        actions_tensor[11::17]=(_actions[:,8]-0.5) * 2 * self.max_effort_pos #11 #RF POS 
        actions_tensor[12::17]= (_actions[:,9]-0.5) * 2 * self.max_effort_vel #12 #RF DRIVE
        actions_tensor[13::17]=(_actions[:,10]-0.5) * 2 * self.max_effort_pos #13 #RM POS
        actions_tensor[14::17]=(_actions[:,11]-0.5) * 2 * self.max_effort_vel #14 #RM DRIVE
        # actions_tensor[1::17]=pos[0]  #1  #LF POS
        # actions_tensor[2::17]=vel[0] #2  #LF DRIVE
        # actions_tensor[3::17]=pos[1] #3  #LM POS
        # actions_tensor[4::17]=vel[0] #4  #LM DRIVE
        # actions_tensor[6::17]=pos[2] #6  #LR POS
        # actions_tensor[7::17]=vel[0] #7  #LR DRIVE
        # actions_tensor[8::17]=pos[3]#8  #RR POS
        # actions_tensor[9::17]=vel[0] #9  #RR DRIVE
        # actions_tensor[11::17]=pos[4] #11 #RF POS 
        # actions_tensor[12::17]=vel[0] #12 #RF DRIVE
        # actions_tensor[13::17]=pos[5]#13 #RM POS
        # actions_tensor[14::17]=vel[0] #14 #RM DRIVE
        # speed =10
        # actions_tensor[0] = 100 #BOTH REAR DRIVE        # actions_tensor[3] = 0
        # actions_tensor[2] = speed 
        # actions_tensor[4] = speed 
        # actions_tensor[7] = speed 
        # actions_tensor[9] = speed 
        # actions_tensor[12] = speed
        
        # actions_tensor[14] = speed
        # 
        self.gym.set_dof_velocity_target_tensor(self.sim, gymtorch.unwrap_tensor(actions_tensor)) #)
        self.gym.set_dof_position_target_tensor(self.sim, gymtorch.unwrap_tensor(actions_tensor)) #)
        #forces = gymtorch.unwrap_tensor(actions_tensor)
        #self.gym.set_dof_actuation_force_tensor(self.sim, forces)
        pass
        
    def post_physics_step(self):
        # implement post-physics simulation code here
        #    - e.g. compute reward, compute observations
        self.progress_buf += 1

        self.gym.refresh_actor_root_state_tensor(self.sim)
        self.gym.refresh_dof_state_tensor(self.sim)
        #print(self.vec_root_tensor)
        self.compute_observations()
        self.compute_rewards()

                # Get Image
        environment = self.envs[0]
        camera = self.camera_handles[0]

        points = []
        pcd = o3d.geometry.PointCloud()

        if self.frame_count == 4: # Don't do this every frame, demanding
            self.gym.render_all_camera_sensors(self.sim)
            print("camera render ", self.gym.render_all_camera_sensors(self.sim))

            #depth_buffer = self.gym.get_camera_image(self.sim, environment, camera, gymapi.IMAGE_DEPTH)
            #self.gym.get_camera_image(self.sim, environment, camera, gymapi.IMAGE_COLOR)
            #rgb_filename1 = "color_cam.png"
            #rgb_filename2 = "depth_cam.png"
            #self.gym.write_camera_image_to_file(self.sim, environment, camera, gymapi.IMAGE_COLOR, rgb_filename1)
            #self.gym.write_camera_image_to_file(self.sim, environment, camera, gymapi.IMAGE_DEPTH, rgb_filename2)
        
            # Get the camera view matrix and invert it to transform points from camera to world space
            #vinv = np.linalg.inv(np.matrix(self.gym.get_camera_view_matrix(self.sim, environment, camera)))

            # Get the camera projection matrix and get the necessary scaling
            # coefficients for deprojection
            # proj = self.gym.get_camera_proj_matrix(self.sim, environment, camera)
            # fu = 2/proj[0, 0]
            # fv = 2/proj[1, 1]
            
            # centerU = self.camerawidth/2
            # centerV = self.cameraheight/2

            # for i in range(self.camerawidth): 
            #     for j in range(self.cameraheight):
            #         if depth_buffer[j, i] != -inf: # ignore empty space 
            #             u = -(i-centerU)/(self.camerawidth)  # image-space coordinate
            #             v = (j-centerV)/(self.cameraheight)  # image-space coordinate
            #             d = depth_buffer[j, i]  # depth buffer value
            #             X2 = [d*fu*u, d*fv*v, d, 1]  # deprojection vector
            #             p2 = X2*vinv  # Inverse camera view to get world coordinates
            #             points.append([p2[0, 0], p2[0, 1], p2[0, 2]])
            # self.frame_count = 0
            # pcd.points = o3d.utility.Vector3dVector(np.array(points))
            # #o3d.visualization.draw_geometries([pcd]) #Print point cloud
           
        self.frame_count = self.frame_count +1

    def compute_observations(self):
        self.obs_buf[..., 0:3] = (self.target_root_positions - self.root_positions) / 3
        self.obs_buf[..., 3:7] = self.root_quats
        self.obs_buf[..., 7:10] = self.root_linvels / 2
        self.obs_buf[..., 10:13] = self.root_angvels / math.pi
        return self.obs_buf


    def compute_rewards(self):
        self.rew_buf[:], self.reset_buf[:] = compute_exomy_reward(
            self.root_positions,
            self.target_root_positions,
            self.reset_buf, self.progress_buf, self.max_episode_length)        

@torch.jit.script
def compute_exomy_reward(root_positions, target_root_positions, reset_buf, progress_buf, max_episode_length):
    # type: (Tensor, Tensor, Tensor, Tensor, float) -> Tuple[Tensor, Tensor]
    # distance to target
    target_dist = torch.sqrt(torch.square(target_root_positions - root_positions).sum(-1))
    pos_reward = 1.0 / (1.0 + target_dist * target_dist)

    reward = pos_reward
    #print(reward[0:5])
    ones = torch.ones_like(reset_buf)
    die = torch.zeros_like(reset_buf)
    # resets due to episode length
    reset = torch.where(progress_buf >= max_episode_length - 1, ones, die)
    return reward, reset        

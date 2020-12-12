#!/usr/bin/python

import rospy
import os
import re
import roslaunch
import rospkg
import copy
import tempfile
import signal
import time
import yaml
import csv
import sys

from mrs_msgs.srv import String as StringSrv
from mrs_msgs.srv import StringResponse as StringSrvResponse
from std_srvs.srv import Trigger, TriggerResponse
from gazebo_msgs.srv import DeleteModel
from gazebo_msgs.msg import ModelStates

VEHICLE_BASE_PORT = 14000
MAVLINK_TCP_BASE_PORT = 4560
MAVLINK_UDP_BASE_PORT = 14560
LAUNCH_BASE_PORT = 14900
DEFAULT_VEHICLE_TYPE = 't650'
VEHICLE_TYPES = ['f450', 'f550', 't650', 'eaglemk2']
SPAWNING_DELAY_SECONDS = 6

# #{
def print_error(string):
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    print(FAIL + string + ENDC)

def print_info(string):
    BOLD = '\033[1m'
    ENDC = '\033[0m'
    print(BOLD + string + ENDC)

def print_ok(string):
    OKGREEN = '\033[92m'
    ENDC = '\033[0m'
    print(OKGREEN + string + ENDC)
# #}

def is_number(string):
    try:
        num = float(string)
    except:
        return False
    return True

def rinfo(message):
    rospy.loginfo('[DroneSpawner]: ' + message)

def rwarn(message):
    rospy.logwarn('[DroneSpawner]: ' + message)

def rerr(message):
    rospy.logerr('[DroneSpawner]: ' + message)


class MrsDroneSpawner:

    def __init__(self, show_help=False, verbose=False):

        self.verbose=verbose

        rospack = rospkg.RosPack()
        pkg_path = rospack.get_path('mrs_simulation')
        path_to_launch_file_firmware = pkg_path + os.sep + 'config' + os.sep + 'spawner_params.yaml'
        self.spawner_params = yaml.safe_load(open(path_to_launch_file_firmware, 'r'))

        if not self.params_integrity_ok():
            return

        if show_help:
            self.print_help()
            return

        # convert spawner_params to default_model_config (to get rid of help strings)
        self.default_model_config = {}
        for key, values in self.spawner_params.items():
            self.default_model_config[key] = values[0]

        self.got_gazebo_model_states = False;
        self.assigned_ids = {} # {id: process_handle}
        self.path_to_launch_file_firmware = pkg_path + os.sep + 'launch' + os.sep + 'run_simulation_firmware.launch'
        self.path_to_launch_file_spawn_model = pkg_path + os.sep + 'launch' + os.sep + 'spawn_simulation_model.launch'
        self.path_to_launch_file_mavros = pkg_path + os.sep + 'launch' + os.sep + 'run_simulation_mavros.launch'

        rospy.init_node('mrs_drone_spawner', anonymous=True)
        rinfo('Node initialization started. All parameters loaded correctly.')
        rospy.Subscriber("~gazebo_model_states", ModelStates, self.callback_gazebo_model_states, queue_size=1)

        if self.verbose:
            rinfo('Loaded the following params:')
            for param, value in self.spawner_params.items():
                print('\t\t' + str(param) + ': ' + str(value))
            print('')
            rinfo('remove arg \'verbose\' in mrs_drone_spawner.launch to stop listing params on startup')

        spawn_server = rospy.Service('~spawn', StringSrv, self.callback_spawn)
        delete_server = rospy.Service('~delete', StringSrv, self.callback_delete)
        self.delete_gazebo_proxy = rospy.ServiceProxy('gazebo/delete_model', DeleteModel)
        rospy.spin()

    # #{ params_integrity_ok
    def params_integrity_ok(self):
        for pname, pvals in self.spawner_params.items():
            if not isinstance(pvals, list):
                print('Error occured while parsing \'spawner_params.yaml\' at parameter \'' + str(pname) + '\'!')
                print('Expected: \'parameter: [default_value, help_string, [vehicle_types]]')
                print('Found: \'' + str(pname) + ': ' + str(pvals) + '\'')
                return False
            elif len(pvals) != 3:
                print('Error occured while parsing \'spawner_params.yaml\' at parameter \'' + str(pname) + '\'!')
                print('Expected: \'parameter: [default_value, help_string, [vehicle_types]]')
                print('Found: \'' + str(pname) + ': ' + str(pvals) + '\'')
                return False
            else:
                try:
                    str(pvals[1])
                except:
                    print('Error occured while parsing \'spawner_params.yaml\' at parameter \'' + str(pname) + '\'!')
                    print('Expected: \'parameter: [default_value, help_string, [vehicle_types]]')
                    print('Parameter does not have a valid help_string')
                    return False
                if not isinstance(pvals[2], list):
                    print('Error occured while parsing \'spawner_params.yaml\' at parameter \'' + str(pname) + '\'!')
                    print('Expected: \'parameter: [default_value, help_string, [vehicle_types]]')
                    print('Found: \'' + str(pvals[2]) + '\' instead of a vehicle_types list')
                    return False
                else:
                    for vehicle_type in pvals[2]:
                        if vehicle_type not in VEHICLE_TYPES:
                            print('Error occured while parsing \'spawner_params.yaml\' at parameter \'' + str(pname) + '\'!')
                            print('Expected: \'parameter: [default_value, help_string, [vehicle_types]]')
                            print('Unkown vehicle type \'' + str(vehicle_type) + '\'')
                            return False
        return True
    # #}

    # #{ assign_free_id
    def assign_free_id(self):
        for i in range(0,251):
            if i not in self.assigned_ids.keys():
                return i
        raise Exception('Cannot assign a free ID to the vehicle!')
    # #}

    # #{ get_ids
    def get_ids(self, params_list):
        requested_ids = []

        # read params until non-numbers start comming
        for p in params_list:
            if is_number(p):
                requested_ids.append(int(p))
            else:
                break

        if len(requested_ids) < 1:
            free_id = self.assign_free_id()
            requested_ids.append(free_id)
            rwarn('Vehicle ID not specified. Number ' + str(free_id) + ' assigned automatically.')
            return requested_ids

        rinfo('Requested vehicle IDs: ' + str(requested_ids))

        ids = []
        # remove all IDs that are already assigned or out of range
        for ID in requested_ids:
            if ID > 249:
                rwarn('Cannot spawn uav' + str(ID) + ', ID out of range <0, 250>!')
                continue

            if ID in self.assigned_ids.keys():
                rwarn('Cannot spawn uav' + str(ID) + ', ID already assigned!')
                continue
            ids.append(ID)

        if len(ids) < 1:
            raise Exception('No valid ID provided')

        return ids
    # #}

    # #{ get_vehicle_type
    def get_vehicle_type(self, params_list):
        vehicle_type = DEFAULT_VEHICLE_TYPE
        for p in params_list:
            for v in VEHICLE_TYPES:
                if v in p:
                    vehicle_type = v
                    break
        return vehicle_type
    # #}

    # #{ get_params_dict
    def get_params_dict(self, params_list, vehicle_type):
        params_dict = copy.deepcopy(self.default_model_config)
        custom_params = {}
        for i, p in enumerate(params_list):
            if '--' in p:
                param_name = p[2:]
                param_name = param_name.replace('-', '_')
                if param_name not in self.default_model_config.keys() and param_name not in VEHICLE_TYPES:
                    raise Exception('Param \'' + str(param_name) + '\' not recognized!')
                children = []
                for j in range(i+1, len(params_list)):
                    if '--' not in params_list[j]:
                        children.append(params_list[j])
                    else:
                        break
                if len(children) < 1:
                    custom_params[param_name] = True
                elif len(children) == 1:
                    custom_params[param_name] = children[0]
                else:
                    custom_params[param_name] = children

        if len(custom_params.keys()) > 0:
            rinfo('Customized params:')
            for pname, pval in custom_params.items():
                if pname not in VEHICLE_TYPES:
                # check if the customized param is allowed for the desired vehicle type
                    allowed_vehicle_types = self.spawner_params[pname][2]
                # print('For param ' + str(pname) + ' allowed: ' + str(allowed_vehicle_types))
                    if vehicle_type not in allowed_vehicle_types:
                        raise Exception('Param \'' + str(pname) + '\' cannot be used with vehicle type \'' + str(vehicle_type) + '\'!')

                print('\t' + str(pname) + ': ' + str(pval))
            params_dict.update(custom_params)
        return params_dict
    # #}

    # #{ get_comm_ports
    def get_comm_ports(self, ID):
        '''
        NOTE
        ports have to match with values assigned in
        mrs_simulation/ROMFS/px4fmu_common/init.d-posix/rcS
        '''
        ports = {}
        ports['udp_offboard_port_remote'] = VEHICLE_BASE_PORT + (4 * ID) + 2
        ports['udp_offboard_port_local'] = VEHICLE_BASE_PORT + (4 * ID) + 1
        ports['mavlink_tcp_port'] = MAVLINK_TCP_BASE_PORT + ID
        ports['mavlink_udp_port'] = MAVLINK_UDP_BASE_PORT + ID
        ports['fcu_url'] = 'udp://:' + str(ports['udp_offboard_port_remote']) + '@localhost:' + str(ports['udp_offboard_port_local'])
        return ports
    # #}

    # #{ get_spawn_poses_from_ids
    def get_spawn_poses_from_ids(self, ids):
        spawn_poses = {}
        for ID in ids:
            inteam_id = ID % 100
            x = 0
            y = 29.45
            z = 0.3
            heading = 0

            if ( inteam_id > 1 ):
                y -= 8*((inteam_id)//2)

            if( inteam_id % 2 == 0 ):
                x += 4
            if( inteam_id % 2 == 1 ):
                x -= 4

            spawn_poses[ID] = {'x': x, 'y': y, 'z': z, 'heading': heading}
        return spawn_poses
    # #}

    # #{ get_spawn_poses_from_args
    def get_spawn_poses_from_args(self, pose_vec4, uav_ids):
        spawn_poses = {}
        x = float(pose_vec4[0])
        y = float(pose_vec4[1])
        z = float(pose_vec4[2])
        heading = float(pose_vec4[3])

        spawn_poses[uav_ids[0]] = {'x': x, 'y': y, 'z': z, 'heading': heading}

        if len(uav_ids) > 1:
            for i in range(len(uav_ids)):
                x += 2
                spawn_poses[uav_ids[i]] = {'x': x, 'y': y, 'z': z, 'heading': heading}

        return spawn_poses
    # #}

    # #{ get_spawn_poses_from_file
    def get_spawn_poses_from_file(self, filename, uav_ids):
        if not os.path.isfile(filename):
            raise Exception('File \'' + str(filename) + '\' does not exist!')

        spawn_poses = {}
        if filename.endswith('.csv'):
            array_string = list(csv.reader(open(filename)))
            for row in array_string:
                if (len(row)!=5):
                    raise Exception('Incorrect data in file \'' + str(filename) +'\'! Data in \'.csv\' file type should be in format [id, x, y, z, heading] (example: int, float, float, float, float)')
                if int(row[0]) in uav_ids:
                    spawn_poses[int(row[0])] = {'x' : float(row[1]), 'y' : float(row[2]), 'z' : float(row[3]), 'heading' : float(row[4])}
                else:
                    raise Exception('File requires UAV ID \'' + str(row[0]) + '\' which was not assigned by the spawn command!')

        elif filename.endswith('.yaml'):
            dict_vehicle_info = yaml.safe_load(open(filename, 'r'))
            for item, data in dict_vehicle_info.items():
                if (len(data.keys())!=5):
                    raise Exception('Incorrect data in file \'' + str(filename) + '\'! Data  in \'.yaml\' file type should be in format \n uav_name: \n\t id: (int) \n\t x: (float) \n\t y: (float) \n\t z: (float) \n\t heading: (float)')

                if int(data['id']) in uav_ids:
                    spawn_poses[data['id']] = {'x' : float(data['x']), 'y' : float(data['y']), 'z' : float(data['z']), 'heading' : float(data['heading'])}
                else:
                    raise Exception('File requires UAV ID \'' + str(dict_vehicle_info[item]['id']) + '\' which was not assigned by the spawn command!')

        else:
            raise Exception('Incorrect file format, must be either \'.csv\' or \'.yaml\'')

        rinfo('Spawn poses returned:')
        rinfo(str(spawn_poses))
        return spawn_poses
    # #}

    # #{ parse_input_params
    def parse_input_params(self, data):
        params_list = data.split()

        try:
            uav_ids = self.get_ids(params_list)
            vehicle_type = self.get_vehicle_type(params_list)
            params_dict = self.get_params_dict(params_list, vehicle_type)
            if params_dict['pos_file'] != 'None':
                rinfo('Loading spawn poses from file \'' + str(params_dict['pos_file']) + '\'')
                spawn_poses = self.get_spawn_poses_from_file(params_dict['pos_file'], uav_ids)
            elif params_dict['pos'] != 'None':
                rinfo('Using spawn poses provided from command line args \'' + str(params_dict['pos']) + '\'')
                spawn_poses = self.get_spawn_poses_from_args(params_dict['pos'], uav_ids)
            else:
                rinfo('Assigning default spawn poses')
                spawn_poses = self.get_spawn_poses_from_ids(uav_ids)

        except Exception as e:
            rerr('Exception raised while parsing user input:')
            rerr(str(e.args[0]))
            raise Exception('Cannot spawn vehicle. Reason: ' + str(e.args[0]))

        params_dict['uav_ids'] = uav_ids
        params_dict['vehicle_type'] = vehicle_type
        params_dict['spawn_poses'] = spawn_poses
        return params_dict
    # #}

    # #{ generate_launch_args
    def generate_launch_args(self, params_dict):

        args_sequences = []
        num_uavs = len(params_dict['uav_ids'])

        for n in range(num_uavs):
            uav_args_sequence = []
            ID = params_dict['uav_ids'][n]
            # get vehicle ID number
            uav_args_sequence.append('ID:=' + str(ID))

            # get vehicle type
            uav_args_sequence.append('vehicle:=' + params_dict['vehicle_type'])

            # setup communication ports
            comm_ports = self.get_comm_ports(ID)
            for name,value in comm_ports.items():
                uav_args_sequence.append(str(name) + ':=' + str(value))

            # setup vehicle spawn pose
            uav_args_sequence.append('x:=' + str(params_dict['spawn_poses'][ID]['x']))
            uav_args_sequence.append('y:=' + str(params_dict['spawn_poses'][ID]['y']))
            uav_args_sequence.append('z:=' + str(params_dict['spawn_poses'][ID]['z']))
            uav_args_sequence.append('heading:=' + str(params_dict['spawn_poses'][ID]['heading']))

            # generate a yaml file for the custom model config
            fd, path = tempfile.mkstemp(prefix='simulation_', suffix='_uav' + str(ID) + '.yaml')
            with os.fdopen(fd, 'w') as f:
                for pname, pvalue in params_dict.items():
                    f.write(str(pname) + ': ' + str(pvalue).lower() + '\n')
            uav_args_sequence.append('model_config_file:=' + path)


            print('UAV' + str(ID) + ' ARGS_SEQUENCE:')
            print(uav_args_sequence)
            args_sequences.append(uav_args_sequence)
        return args_sequences
    # #}

    # #{ launch_firmware
    def launch_firmware(self, ID, uav_roslaunch_args):
        rinfo('Running firmware for uav' + str(ID) + '...')
        uuid = roslaunch.rlutil.get_or_generate_uuid(None, False)
        roslaunch.configure_logging(uuid)
        roslaunch_sequence = [(self.path_to_launch_file_firmware, uav_roslaunch_args)]
        launch = roslaunch.parent.ROSLaunchParent(uuid, roslaunch_sequence)
        try:
            launch.start()
        except:
            rerr('Error occured while starting firmware for uav' + str(ID) + '!')
            raise Exception('Cannot spawn uav' + str(ID))
        return launch
    # #}

    # #{ spawn_gazebo_model
    def spawn_simulation_model(self, ID, uav_roslaunch_args):
        rinfo('Spawning Gazebo model for uav' + str(ID) + '...')
        uuid = roslaunch.rlutil.get_or_generate_uuid(None, False)
        roslaunch.configure_logging(uuid)
        roslaunch_sequence = [(self.path_to_launch_file_spawn_model, uav_roslaunch_args)]
        launch = roslaunch.parent.ROSLaunchParent(uuid, roslaunch_sequence)
        try:
            launch.start()
        except:
            rerr('Error occured while spawning Gazebo model for uav' + str(ID) + '!')
            raise Exception('Cannot spawn uav' + str(ID))
        return launch
    # #}

    # #{ launch_mavros
    def launch_mavros(self, ID, uav_roslaunch_args):
        rinfo('Running mavros for uav' + str(ID) + '...')
        uuid = roslaunch.rlutil.get_or_generate_uuid(None, False)
        roslaunch.configure_logging(uuid)
        roslaunch_sequence = [(self.path_to_launch_file_mavros, uav_roslaunch_args)]
        launch = roslaunch.parent.ROSLaunchParent(uuid, roslaunch_sequence)
        try:
            launch.start()
        except:
            rerr('Error occured while launching mavros for uav' + str(ID) + '!')
            raise Exception('Cannot spawn uav' + str(ID))
        return launch
    # #}

    # #{ print_help
    def print_help(self):
        BOLDGREEN = '\033[32;1m'
        BOLD = '\033[1m'
        ENDC = '\033[0m'
        print('')
        print(BOLD + '****************************' + ENDC)
        print(BOLD + '** MRS DRONE SPAWNER HELP **' + ENDC)
        print(BOLD + '****************************' + ENDC)
        print('')
        print('The mrs_drone_spawner is a ROS node, which allows you to dynamically add new vehicles into your Gazebo simulation\n')
        print('To spawn a new drone, use:\nrosservice call /mrs_drone_spawner/spawn "...string of arguments..."\n')
        print('Available arguments:')
        print(BOLDGREEN + '1 2 3' + ENDC + ' ... : use numbers ' + BOLD + '[0 to 250]' + ENDC + ' to assign IDs to the vehicles. The vehicles will be named \'uav1, uav2, uav3 ... \'')
        print('  ' + BOLDGREEN + ':' + ENDC + ' using a blank space instead of a number will automatically assign an unused ID to the vehicle')
        
        for param, data in self.spawner_params.items():
            
            default_value, help_string, vehicle_types = data
            print(BOLDGREEN + '--' + str(param).replace('_', '-') + ENDC + BOLD + ' (default: ' + str(default_value) + ')' + ENDC + ': ' + str(help_string) )
    # #}

    # #{ callback_gazebo_model_states
    def callback_gazebo_model_states(self, msg):
        self.got_gazebo_model_states = True
    # #}

    # #{ callback_spawn
    def callback_spawn(self, req):
        if not self.got_gazebo_model_states:
            res = StringSrvResponse()
            res.success = False
            res.message = str('Gazebo model state topic not found. Is Gazebo running?')
            return res

        params_dict = None
        try:
            params_dict = self.parse_input_params(req.value)
        except Exception as e:
            res = StringSrvResponse()
            res.success = False
            res.message = str(e.args[0])
            return res

        if params_dict is None:
            res = StringSrvResponse()
            res.success = False
            res.message = str('Cannot process input parameters')
            return res

        roslaunch_args = None
        try:
            roslaunch_args = self.generate_launch_args(params_dict)
        except Exception as e:
            res = StringSrvResponse()
            res.success = False
            res.message = str(e.args[0])
            return res

        if roslaunch_args is None:
            res = StringSrvResponse()
            res.success = False
            res.message = str('Cannot generate roslaunch arguments')
            return res

        for ID in params_dict['uav_ids']:
            self.assigned_ids[ID] = None

        rinfo('Spawning ' + str(len(params_dict['uav_ids'])) + ' vehicles of type \'' + params_dict['vehicle_type'] + '\'')

        orig_signal_handler = roslaunch.pmon._init_signal_handlers
        roslaunch.pmon._init_signal_handlers = self.dummy_function

        successful_spawns = 0
        unsuccessful_spawns = 0
        # iterate to get sequences for individual UAVs
        for i, uav_roslaunch_args in enumerate(roslaunch_args):
            launched_processes = []
            ID = params_dict['uav_ids'][i]
            try:
                firmware_process = self.launch_firmware(ID, uav_roslaunch_args)
                launched_processes.append(firmware_process)
                gz_spawning_process = self.spawn_simulation_model(ID, uav_roslaunch_args)
                launched_processes.append(gz_spawning_process)
                mavros_process = self.launch_mavros(ID, uav_roslaunch_args)
                launched_processes.append(mavros_process)

            except Exception as e:
                unsuccessful_spawns += 1
                del self.assigned_ids[ID]
                rerr(str(e.args[0]))
                continue

            self.assigned_ids[ID] = launched_processes
            if len(roslaunch_args) > 1 and i < len(roslaunch_args) - 1:
                time.sleep(SPAWNING_DELAY_SECONDS)
            successful_spawns += 1

        res = StringSrvResponse()
        res.success = unsuccessful_spawns == 0
        res.message = 'Spawned ' + str(successful_spawns) + ' vehicles'
        roslaunch.pmon._init_signal_handlers = orig_signal_handler
        return res
    # #}

    # #{ callback_delete
    def callback_delete(self, req):
        params_list = req.value.split()
        num_errors = 0
        ids_to_delete = []
        for p in params_list:
            if is_number(p):
                ids_to_delete.append(int(p))
            else:
                res = StringSrvResponse()
                res.success = False
                res.message = 'Invalid vehicle ID: ' + str(p)
                return res

        successfully_deleted = 0
        rinfo('Will delete these vehicles: ' + str(ids_to_delete))
        for ID in ids_to_delete:
            plugins_killed = self.kill_plugins(ID)
            model_deleted = self.delete_model(ID)
            if plugins_killed and model_deleted:
                successfully_deleted += 1
                del self.assigned_ids[ID]

        res = StringSrvResponse()
        res.success = successfully_deleted == len(ids_to_delete)
        res.message = str('Deleted ' + str(successfully_deleted) + ' vehicles')
        return res
    # #}

    # #{ kill_plugins
    def kill_plugins(self, ID):
        if ID not in self.assigned_ids.keys():
            rerr('Cannot kill plugins of \'uav' + str(ID) + '\'. Vehicle not found')
            return False

        rinfo('Killing plugins of \'uav' + str(ID) + '\'...')
        try:
            for process in self.assigned_ids[ID]:
                process.shutdown()
        except:
            rerr('Cannot kill plugins of \'uav' + str(ID) + '\'')
            return False
        rinfo('Plugins for \'uav' + str(ID) + '\' killed')
        return True
    # #}

    # #{ delete_model
    def delete_model(self, ID):
        delete_result = self.delete_gazebo_proxy('uav' + str(ID))
        if not delete_result.success:
            rerr('Cannot delete model \'uav' + str(ID) + '\'. Reason: ' + str(delete_result.status_message))
        else:
            rinfo('Model \'uav' + str(ID) + '\' deleted')
        return delete_result.success
    # #}

    def dummy_function(self):
        pass

if __name__ == '__main__':

    show_help = True
    if 'no_help' in sys.argv:
        show_help = False
    
    verbose = 'verbose' in sys.argv
    
    try:
        spawner = MrsDroneSpawner(show_help, verbose)
    except rospy.ROSInterruptException:
        pass

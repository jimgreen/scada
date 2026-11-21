"""Public implementation contracts, independent of schema field availability.

These describe this bundled runtime, not certification or connected hardware.
"""
from copy import deepcopy


def validate_iec104_runtime_type(type_id):
    from core.iec104_python.codec import MONITOR, COMMAND
    if int(type_id) not in MONITOR | COMMAND:
        raise ValueError(f'当前 IEC104 引擎不支持类型 {type_id}')


def protocol_capabilities(component):
    if component not in {'scdfe_c', 'scdfe_s', 'scdfe_t'}:
        raise ValueError('未知组件')
    protocols = {
        'iec104': {
            'implementation': 'python-stdlib', 'native_dependency': False,
            'general_interrogation': True, 'spontaneous': True, 'periodic': True,
            'monitoring_semantics': 'latest_point_snapshot_and_event_journal',
            'soe_event_log': True, 'soe_lossless_forwarding': False,
            'soe_acknowledged_forwarding': component in {'scdfe_s','scdfe_t'},
            'soe_forward_types': [30,31],
            'soe_event_causes': [3],
            'soe_delivery': 'at_least_once_apci_ack',
            'soe_live_client_isolation': component in {'scdfe_s','scdfe_t'},
            'soe_reconnect_policy': 'conservative_shared_floor_replay_duplicates_possible',
            'soe_native_requirement': None,
            'soe_cp56_time_convention': 'UTC_calendar_raw_bytes_preserved',
            'soe_max_asdu_length_enforced': True,
            'direct_execute': True, 'select_and_execute': True,
            'separate_select': False, 'cancel_yk': True, 'cancel_yt': True,
            'cancel_scope': 'local_selection_only' if component == 'scdfe_t' else 'protocol_deactivation',
            'time_tags': ['NONE', 'CP24Time2a', 'CP56Time2a'], 'cp24': True,
            'cp24_hour_reference': 'nearest_UTC_hour_at_receive',
            'u_testfr': 'python_t3_timer', 'explicit_u_testfr': component == 'scdfe_c',
            'application_test_command': component == 'scdfe_c',
            'max_asdu_length_scope': 'transport_enforced',
            'native_max_asdu_length': 249,
            'confirmation_scope': 'database_commit' if component == 'scdfe_t' else
                                  'simulator_database' if component == 'scdfe_s' else 'remote_acceptance',
        },
        'modbus-tcp': {'function_codes': [1,2,3,4,5,6,15,16],
                       'address_bases': [0,1,10001,30001,40001],
                       'confirmation_scope': 'database_commit' if component == 'scdfe_t' else 'remote_response'},
    }
    if component == 'scdfe_s':
        protocols['modbus-tcp'].update(
            engineering_value_mapping=True, register_slices=True,
            shared_register_slices='disjoint_masks_only', inverted_bits=True,
            register_yk=True, atomic_request_database_write=True,
            confirmation_scope='simulator_database')
    if component == 'scdfe_t':
        protocols['modbus-tcp'].update(atomic_request_database_write=True,
                                      requires_c_process=False)
    if component != 'scdfe_t':
        protocols['modbus-rtu'] = deepcopy(protocols['modbus-tcp'])
        protocols['can'] = {
            'standard': True, 'extended': True, 'fd': 'driver_dependent',
            'signal_mapping': True, 'dbc_file_parser': False,
            'database_periodic_frames': True, 'database_replay_groups': True,
            'canopen_stack': False, 'j1939_stack': False, 'uds_stack': False,
            'physical_diagnostics': 'hardware_dependent',
            'fault_injection': 'hardware_dependent',
        }
        protocols['bacnet'] = {
            'transport': 'udp',
            'object_types': ['analog-input', 'analog-output', 'binary-input', 'binary-output'],
            'read_property': True,
            'write_property': True,
            'simulation_mode': component == 'scdfe_s',
            'confirmation_scope': 'simulator_database' if component == 'scdfe_s' else 'remote_response',
        }
        protocols['opc'] = {
            'transport': 'opc-ua-tcp',
            'batch_read': True,
            'write_value': True,
            'node_id_mapping': True,
            'simulation_mode': component == 'scdfe_s',
            'confirmation_scope': 'simulator_database' if component == 'scdfe_s' else 'remote_response',
        }
    return {'component': component, 'protocols': protocols,
            'scope': 'implementation_capabilities_not_device_certification'}

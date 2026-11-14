"""Public implementation contracts, independent of schema field availability.

These describe this bundled runtime, not certification or connected hardware.
"""
from copy import deepcopy


def validate_iec104_runtime_type(type_id):
    if int(type_id) in {2, 4, 6, 10, 12, 14}:
        raise ValueError('当前 c104 驱动不支持 CP24Time2a 监视类型；请明确配置 NONE 或 CP56Time2a')


def protocol_capabilities(component):
    if component not in {'scdfe_c', 'scdfe_s', 'scdfe_t'}:
        raise ValueError('未知组件')
    protocols = {
        'iec104': {
            'general_interrogation': True, 'spontaneous': True, 'periodic': True,
            'monitoring_semantics': 'latest_point_snapshot_and_event_journal',
            'soe_event_log': True, 'soe_lossless_forwarding': False,
            'soe_acknowledged_forwarding': component in {'scdfe_s','scdfe_t'},
            'soe_forward_types': [30,31],
            'soe_event_causes': [3],
            'soe_delivery': 'at_least_once_apci_ack',
            'soe_native_requirement': '2.2.1+scada.3',
            'soe_cp56_time_convention': 'UTC_calendar_raw_bytes_preserved',
            'soe_max_asdu_length_enforced': True,
            'direct_execute': True, 'select_and_execute': True,
            'separate_select': False, 'cancel_yk': True, 'cancel_yt': True,
            'cancel_scope': 'local_selection_only' if component == 'scdfe_t' else 'protocol_deactivation',
            'time_tags': ['NONE', 'CP56Time2a'], 'cp24': False,
            'u_testfr': 'native_t3_timer', 'explicit_u_testfr': False,
            'application_test_command': component == 'scdfe_c',
            'max_asdu_length_scope': 'diagnostic_threshold',
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
    return {'component': component, 'protocols': protocols,
            'scope': 'implementation_capabilities_not_device_certification'}

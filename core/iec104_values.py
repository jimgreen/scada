"""IEC104 double-point wire states at the business-value boundary.

The wire encoding is INTERMEDIATE=0, OFF=1, ON=2, INDETERMINATE=3.
Database values and application command callbacks use False/True instead.
Unknown monitoring states remain None and must carry invalid quality; they
are never accepted as executable commands.
"""


def double_to_binary(value):
    state = int(value)
    if state == 1:
        return False
    if state == 2:
        return True
    return None


def binary_command_value(value):
    # c104 is optional for deployments using another protocol, so this shared
    # value helper must not import the native extension at module import time.
    if type(value).__name__ == 'Double':
        value = double_to_binary(value)
    if isinstance(value, (bool, int, float)) and value in (0, 1):
        return bool(value)
    raise ValueError('遥控值必须是 0/1，双点命令只能为 OFF/ON')

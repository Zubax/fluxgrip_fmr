export UAVCAN__CAN__IFACE="socketcan:slcan0"
export UAVCAN__CAN__MTU=8
export UAVCAN__NODE__ID=$(yakut accommodate)
echo "Node ID: $UAVCAN__NODE__ID"
echo "Using CAN interface: $UAVCAN__CAN__IFACE"

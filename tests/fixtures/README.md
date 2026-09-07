# Protocol fixtures

These frames are synthetic; they are not captures from the Raspberry Pi.
Victron frames use the public all-zero 16-byte TEST key and nonce 1. They encode
known values using the field layout in victron-ble 0.9.3 (SolarCharger, Inverter,
SmartBatteryProtect), encrypted with AES-CTR. The tests decrypt the fixed bytes
through the installed library and check JSON and scalar MQTT publications.
Temperature frames cover Ruuvi format 5, ThermoBeacon, Inkbird 9/18-byte frames
and the authoritative FFF2 GATT value. No production encryption keys are stored.

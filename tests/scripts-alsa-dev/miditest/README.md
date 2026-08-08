# MIDI Test

Objective is to perform some tests on the MIDI in and output capabilities of the Reloop Jockey 3 hardware
initial testing using Mixxx with my new Reloop Jockey 3 driver suggests that there may be a bottleneck in the MIDI output performance.

To validate this script will perform tests to measure the maximum number of MIDI messages that can be received from or sent to the device



Environment: 
- baseline measurements: python on Windows 11 
- new driver confirmation: python on Debian Linux 13



## Test results:

### Window 11 with Vendor driver 2.9.73

fast moving of input controls/dials/faders/jog wheels;
have been able to achieve peak rate of 1695 bytes/s 

transmit rate: sustained 330 bytes/second 
peak 862 bytes/s (may be buffering in the OS?)
sustained maximum 515 bytes/s (while flooding the interface)


# Jockey 3 Hardware

Analysis based on the Service Manual for the reloop Jockey 3 Master Edition



IC303 - LC78212 analog switch for routing


IC305 - PCM1690 (vout 1,2 -> master out; vout 5,6 -> headphones)  "P" output?
IC306 - PCM1690 (vout 1,2 -> master out; vout 5,6 -> headphones)  "D" output?

IC307 - PCM1804 P1 in L+R
IC308 - PCM1804 D1 in L+R
IC309 - PCM1803A - mono signal to L+R in for microphone ADC

IC320 - PCM1804 P2 in L+R
IC321 - PCM1804 D2 in L+R


IC710 DSP56374 is used for the stand-alone mixing; connects to the "D" tagged converters  --> "DSP"



IC9 - ISP1583BS - USB controller
IC10 - MEGA8515 - the "ploytec" magic? 


The USB/"Ploytec" part of the schematic is interesting;
IC7 (74HC4050) sends 2 outputs from the MEGA8515 AD0, AD1 to the two output DACs, SDO0 and SDO1; NOTE:  the schematic has 6 more outputs (SDO2..7) which are not populated

the 3 input ADCs SDI0,SDI1,SDI2 are directly connected to the ISP1583 ; NOTE: the schematic as in total 16 inputs defined (SDI0...SDI15)

These SDIxx/SDOxx signals go the ADC/DAC with the "P" tag --> "Ploytec"?

So it looks like the device has actually two parallel paths, with duplicated ADC/DAC;  one set for the "DSP", and other set for "Ploytec"


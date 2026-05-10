## py-hpcs

Scripts to capture spectrum with Hopoocolor HPCS-310 spectrometers.

`hpcs-capture.py` to capture spectrum to csv.

`argyll.py` to run ArgyllCMS commands like `dispread` with the spectrometer.
If you want to Argyll to use the spectrum (instead of just XYZ), you'll need a [patched Argyll](https://github.com/andrewcchen/Argyll).

`to-colorcalculator.py` to convert spectrum csv to osram color calculator compatible txt file.

### Argyll examples (patched Argyll required)

`argyll.py ccxxmake -S -t s filename` to create a ccss file with the display's spectrum.
`argyll.py ccxxmake -t s filename` to create a ccmx file with correction matrix for a colorimeter. Take colorimeter measurements first, then select the external instrument for the spectrometer.
`dispread -s -y l filename` to take readings creating a ti3 file.

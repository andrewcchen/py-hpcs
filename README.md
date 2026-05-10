## py-hpcs

Scripts to capture spectrum with Hopoocolor HPCS-310 spectrometers.

`hpcs-capture.py` to capture spectrum to csv.

`argyll.py` to run ArgyllCMS commands like `dispread` with the spectrometer.
If you want to Argyll to see the spectrum (instead of just XYZ), you'll need a [patched Argyll](https://github.com/andrewcchen/Argyll).

`to-colorcalculator.py` to convert spectrum csv to osram color calculator compatible txt file.

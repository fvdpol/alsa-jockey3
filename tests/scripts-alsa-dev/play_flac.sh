


MYFILE="Music/Collections - Dance Smash Hits/538 Dance Smash Hits 2002 vol. 1 (Winter)/22 - Georgina - Yo Quiero Bailar.flac"
MYFILE="Music/Collections - Dance Smash Hits/538 Dance Smash 2005 vol. 1/08 - Tiësto - Adagio for Strings (Live Video Mix).flac"
MYFILE="Music/Collections - Dance Smash Hits/538 Dance Smash Hits 2003 vol. 3 (Summer)/06 - Junior Jack - E Samba.flac"

echo $MYFILE

ls -lh "$MYFILE"

sox "$MYFILE" -t raw -b 24 -e signed-integer -c 4 - remix 1 2 1 2 | aplay -D hw:RJ3 -f S24_3LE -c 4 -r 44100


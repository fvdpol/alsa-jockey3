sudo journalctl --dmesg | grep -e waited | cut -d ' ' -f 10 | sort -n | uniq -c

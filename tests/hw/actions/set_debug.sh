echo "module snd_reloop_jockey3 +p" | sudo tee /sys/kernel/debug/dynamic_debug/control

# disable printing kernel messages to console 
# especially if serial console is used this causes a massive slowdown and influences timing
sudo dmesg -n 1

# 
echo sudo sysctl -w kernel.printk="8 4 1 7"




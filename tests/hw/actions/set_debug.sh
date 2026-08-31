echo "module snd_reloop_jockey3 +p" | sudo tee /sys/kernel/debug/dynamic_debug/control

# 
echo sudo sysctl -w kernel.printk="8 4 1 7"




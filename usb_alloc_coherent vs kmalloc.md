# usb_alloc_coherent vs kmalloc
2026-06-14

## Question:

On arm64 architecture (Raspberry PI4), i observed issues with CMA errors (running out of contiguous space in a fairly small 64M CMA pool).
One of the the suggested improvements would be to usb_alloc_coherent() instead of kmalloc()/kzalloc(), and use the DMA API.

Explain me if this change would indeed bring an improvement regarding driver stability, and cross-platform support. What are the disadvantages? and how big of a change would it be?

## Response:

✦ Using usb_alloc_coherent() is a standard practice in high-performance or low-latency USB drivers, particularly on architectures like ARM
  (e.g., Raspberry Pi) where cache coherency between the CPU and the USB controller is not always "transparent" or hardware-managed as it is
  on x86.

  Here is an analysis of how this change would impact your driver:

  1. Stability and Cross-Platform Support
   * Cache Coherency: On ARM, if you use kmalloc, the CPU writes to its cache. The USB controller (DMA) reads directly from physical RAM. If
     the cache hasn't been flushed to RAM, the device reads "garbage" or old data. While the USB core usually handles this flushing for you,
     usb_alloc_coherent() provides a "shortcut" by providing memory that is already mapped and synchronized.
   * Fragmentation (CMA): Your observation about CMA errors is the key. kmalloc pulls from the general system slab. If the system is
     fragmented, finding 16–20 contiguous chunks for your URBs can fail. usb_alloc_coherent() pulls from a dedicated pool (often the CMA
     pool). 
   * Cross-platform: This is the most "correct" way to write a driver that works reliably across x86, ARM, and MIPS. It removes the overhead
     of the USB core having to map/unmap and flush caches for every single URB completion.

  2. Disadvantages
   * Memory Waste: usb_alloc_coherent() usually allocates memory in page-sized increments (typically 4KB). Your buffers are likely 512 bytes
     (PLOYTEC_PKT_SIZE). If you allocate 16 URBs individually, you will consume 64KB of CMA memory to store only 8KB of data.
   * Pool Exhaustion: As you noted, the RPi4 CMA pool is often small (64MB) and shared with the GPU, Camera, and Display. If your driver
     starts "hogging" CMA pages, it could cause other subsystems to fail.
   * No devres support: There is no standard devm_usb_alloc_coherent(). You would have to manually manage the lifecycle or create your own
     devres action to free the memory.

  3. Impact of the Change
  The change is moderate in scope. You would need to:
   1. Modify the jockey3_chip struct to store the DMA addresses (dma_addr_t) for each buffer.
   2. Update jockey3_probe:
       * Replace kzalloc with usb_alloc_coherent().
       * Pass the &chip->playback_dma[i] pointer to the allocation function.
   3. Update URB initialization:
       * Set urb->transfer_dma to the address returned.
       * Set urb->transfer_flags |= URB_NO_TRANSFER_DMA_MAP. This tells the USB core: "I've already handled the DMA mapping, don't do it
         again."
   4. Update jockey3_disconnect / Cleanup:
       * Use usb_free_coherent().

  Is it a solution for your CMA errors?
  Actually, if you are already getting CMA errors, switching to usb_alloc_coherent might make them worse because it forces the allocation
  into the CMA pool. However, it makes the failure happen at load time (driver fails to probe) rather than at runtime (random EIO errors
  during playback), which is much better for stability.

  ---

  Implementation Strategy
  If you want to proceed, I recommend "packing" your buffers. Instead of 16 separate allocations, you could allocate one larger coherent
  block and divide it yourself. This saves memory and reduces CMA pressure.

  Would you like me to prepare a plan to migrate the URB buffer management to usb_alloc_coherent? I can also include the "buffer packing"
  optimization to minimize the impact on your 64MB CMA pool.


Decision: put on hold; this would add additional complexity, for relatively limited gains. Possibly revisit later 
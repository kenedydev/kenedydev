```
kenedy@dev:~$ whoami
┌────────────────────────────────────────────────────────┐
│                                                        │
│   ██╗  ██╗███████╗███╗   ██╗███████╗██████╗ ██╗   ██╗  │
│   ██║ ██╔╝██╔════╝████╗  ██║██╔════╝██╔══██╗╚██╗ ██╔╝  │
│   █████╔╝ █████╗  ██╔██╗ ██║█████╗  ██║  ██║ ╚████╔╝   │
│   ██╔═██╗ ██╔══╝  ██║╚██╗██║██╔══╝  ██║  ██║  ╚██╔╝    │
│   ██║  ██╗███████╗██║ ╚████║███████╗██████╔╝   ██║     │
│   ╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝╚══════╝╚═════╝    ╚═╝     │
│                                                        │
│   > firmware | deterministic | bare-metal              │
│                                                        │
└────────────────────────────────────────────────────────┘
```
```
kenedy@dev:~$ cat aboutme
Computer Engineering student at UTFPR & Firmware Developer. Blending rigorous
software engineering with hardware constraints, bridging high-level logic
with the physical limits of silicon.

Work revolves around real-time embedded systems: bare-metal and RTOS,
sensor fusion, high-performance peripheral drivers, and power management.
The MCU family changes; fundamentals don't. Discipline stays the same across
architectures, whether ARM Cortex-M or whatever comes next.

Philosophy is built on determinism and reliability. Firmware is treated like
any serious software: version-controlled, modular, and rigorously validated.
I build Python tooling to accelerate hardware validation and manage scaling.
```
```
kenedy@dev:~$ ./pinout --show && tree ~/expertise
           ┌───────────┐
     C  ─●─┤ 1      20 ├─●─  ThreadX
Python  ─●─┤ 2      19 ├─●─  ARM Cortex-M33
 STM32  ─●─┤ 3      18 ├─●─  STM32H5
   DMA  ─●─┤ 4      17 ├─●─  CMSIS
   I²C  ─●─┤ 5      16 ├─●─  Madgwick
   SPI  ─●─┤ 6      15 ├─●─  RS-485
  UART  ─●─┤ 7      14 ├─●─  ADC
  Bash  ─●─┤ 8      13 ├─●─  Sleep/Standby
   Git  ─●─┤ 9      12 ├─●─  Toolchain
 Linux  ─●─┤ 10     11 ├─●─  GDB/OpenOCD
           └───────────┘

~/expertise
├── rtos/
│   ├── threadx/       # queues, mutexes, semaphores, event flags
│   └── architecture/  # deterministic IPC, priority preemption
├── drivers/
│   ├── serial/        # i2c, spi, uart, i2s, rs-485 + crc
│   ├── dma/           # zero-copy throughput, FIFO batching
│   └── adc/           # signal acquisition & digital filtering
├── signal-processing/
│   ├── madgwick/      # sensor fusion, 3D orientation
│   └── pipelines/     # hardware-accelerated DSP execution
├── power/
│   └── low-power/     # sleep, standby, shutdown, clock-gating
└── tooling/
    ├── telemetry/     # rerun.io real-time visualization
    └── validation/    # automated test rigs & log parsing
```
```
kenedy@dev:~$ ./current_project --info
struct Project {
    .name   = "BIA Radar",
    .url    = "https://biaradar.com",
    .role   = "Subsystem Architecture & Core Firmware Developer",
    
    .stack = {
        .mcu  = "STM32H5 (ARM Cortex-M33)",
        .rtos = "Azure RTOS (ThreadX)"
    },
    
    .milestone = {
        .award = "1st Place National 'Bengalas Inteligentes' Contest",
        .prize = 500000.00 /* BRL */
    }
};
```
<pre>
kenedy@dev:~$ ping -c 4 kenedy.dev
PING kenedy.dev(::1) 56 data bytes
64 bytes from ::1: icmp_seq=1 ttl=64 time=0.033 ms → <a href="https://kenedy.dev">[website]</a>
64 bytes from ::1: icmp_seq=2 ttl=64 time=0.045 ms → <a href="https://www.linkedin.com/in/kenedyk/">[linkedin]</a>
64 bytes from ::1: icmp_seq=3 ttl=64 time=0.021 ms → <a href="https://github.com/kenedydev">[github]</a>
64 bytes from ::1: icmp_seq=4 ttl=64 time=0.038 ms → <a href="mailto:kenedy@kenedy.dev">[email]</a>

--- kenedy.dev ping statistics ---
4 packets transmitted, 4 received, 0% packet loss, time 3074ms
rtt min/avg/max/mdev = 0.021/0.034/0.045/0.008 ms
</pre>

```bash
kenedy@dev:~$ █
```

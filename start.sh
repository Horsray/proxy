#!/bin/bash
cd /root/home/hueying_proxy
/home/miniconda3/envs/proxyenv/bin/python main.py >> /var/log/hueying_proxy.log 2>> /var/log/hueying_proxy.err.log

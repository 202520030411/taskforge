#!/bin/bash
cd /tests && python3 -I -m pytest -q -p no:cacheprovider --rootdir=/tests test_outputs.py

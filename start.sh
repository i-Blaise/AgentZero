#!/bin/bash
cd /Users/blaisemennia1/Documents/Projects/Personal/AgentZero
source venv/bin/activate
uvicorn agentzero.main:app --host 0.0.0.0 --port 8080 --reload

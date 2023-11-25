
from dataclasses import dataclass
import sys
from typing import Dict, List, Optional, Union
from .agent import Agent
from .conversable_agent import ConversableAgent
import logging
from ray.util.client.common import ClientActorHandle, ClientObjectRef
import ray

def get_agent_name(agent: Union[Agent,ClientActorHandle]) -> str:
    if isinstance(agent,Agent):
        agent_name = agent.name
    else:
        agent_name = ray.get(agent.name.remote())

    return agent_name    

def run_agent_func(agent: Union[Agent,ConversableAgent,ClientActorHandle], func_name: str, *args, **kwargs):
    """Run a function of an agent."""
    if isinstance(agent,Agent):
        return getattr(agent, func_name)(*args, **kwargs)
    else:
        return ray.get(getattr(agent, func_name).remote(*args, **kwargs))
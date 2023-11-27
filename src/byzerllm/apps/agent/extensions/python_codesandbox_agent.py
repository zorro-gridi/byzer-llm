from ..conversable_agent import ConversableAgent
from ..agent import Agent
from .. import get_agent_name,run_agent_func,ChatResponse
import ray
from ray.util.client.common import ClientActorHandle, ClientObjectRef
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union
from typing import Callable, Dict, Optional, Union
import time
import sys
import io
import traceback

from ....utils.client import ByzerLLM,ByzerRetrieval,code_utils



class CodeSandbox:
    def __init__(self,file_path:str,file_ref) -> None:        
        self.file_ref = file_ref
        self.file_path = file_path
        self.session_variables = {}
        if self.file_ref:
            if isinstance(self.file_ref,ClientObjectRef):
                content = ray.get(self.file_ref)
            else:
                content = self.file_ref            
            with open(self.file_path, "wb") as f:
                f.write(content)
                
    def set_value(self,name:str,value:str): 
        self.session_variables[name] = value
        return self

    def get_value(self,name:str):
        if name not in self.session_variables:
            return None
        return self.session_variables[name]

    def get_file_path(self):
        return self.file_path        

    def execute_code(self,code)->Tuple[int, str, str]:
        return code_utils.execute_code(
                code = code,
                timeout=30*60,
                filename=None,
                work_dir=None,
                use_docker=False,
                lang="python"        
                ) 
    
    def exec_capture_output(self,code: str,target_names:Dict[str,Any]={}) -> Tuple[int,str,Any]:
        buffer = io.StringIO()
        sys.stdout = buffer
        sys.stderr = buffer

        try:
            variables = {}
            exec(code,variables)
            response = {}
            for name,v in target_names.items():
                if name in variables:
                    response[name] = variables[name]
        except Exception:
            return 1,traceback.format_exc(),{}

        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__

        return 0,buffer.getvalue(),response
    
class PythonSandboxAgent(ConversableAgent):        

    def __init__(
        self,
        name: str,
        llm: ByzerLLM,
        retrieval: ByzerRetrieval,
        system_message: Optional[str],        
        is_termination_msg: Optional[Callable[[Dict], bool]] = None,
        max_consecutive_auto_reply: Optional[int] = None,
        human_input_mode: Optional[str] = "NEVER",
        code_execution_config: Optional[Union[Dict, bool]] = {},
        **kwargs,
    ):
        super().__init__(
            name,
            llm,retrieval,
            system_message,
            is_termination_msg,
            max_consecutive_auto_reply,
            human_input_mode,
            code_execution_config=code_execution_config,            
            **kwargs,
        )
        self.sandboxes = {}
        self.lasted_updated = {}
        
        ## Restore the reply function list
        self._reply_func_list = []        

        ## Register the reply functions                
        self.register_reply([Agent, ClientActorHandle,str], PythonSandboxAgent.generate_execute_code_reply) 
        self.register_reply([Agent, ClientActorHandle,str], ConversableAgent.check_termination_and_human_reply)             
        
    def generate_reply(
        self,
        raw_message: Optional[Union[Dict,str,ChatResponse]] = None,
        messages: Optional[List[Dict]] = None,
        sender: Optional[Union[ClientActorHandle,Agent,str]] = None,
        exclude: Optional[List[Callable]] = None,
    ) -> Union[str, Dict, None,ChatResponse]:
        if all((messages is None, sender is None)):
            error_msg = f"Either {messages=} or {sender=} must be provided."            
            raise AssertionError(error_msg)
        
        print(f"generating reply======",flush=True)
        if messages is None:
            messages = self._messages[get_agent_name(sender)]                

        for reply_func_tuple in self._reply_func_list:
            reply_func = reply_func_tuple["reply_func"]
            if exclude and reply_func in exclude:
                continue
            print(f"======{reply_func}",flush=True)            
            final, reply = reply_func(self, raw_message=raw_message, messages=messages, sender=sender, config=reply_func_tuple["config"])
            if final:                
                return reply
                         
        return self._default_auto_reply 
    

    def generate_execute_code_reply(
        self,
        raw_message: Optional[Union[Dict,str,ChatResponse]] = None,
        messages: Optional[List[Dict]] = None,
        sender: Optional[Union[ClientActorHandle,Agent,str]] = None,
        config: Optional[Any] = None,
    ) -> Tuple[bool, Union[str, Dict, None,ChatResponse]]:
        
        print("Checking code execution...",flush=True)   
        code_execution_config = config if config is not None else self._code_execution_config
        
        if code_execution_config is False:
            print("code_execution_config is False...",flush=True) 
            return False, None
        
        if messages is None:
            messages = self._messages[get_agent_name(sender)]
        
        last_n_messages = code_execution_config.pop("last_n_messages", 1)        

        for i in range(min(len(messages), last_n_messages)):
            message = messages[-(i + 1)]
            if not message["content"]:
                continue
            code_blocks = code_utils.extract_code(message["content"])
            if len(code_blocks) == 1 and code_blocks[0][0] == "unknown":
                continue

            # found code blocks, execute code and push "last_n_messages" back
            #  combine all code blocks into one code block
            codes = [code_block[1] for code_block in code_blocks if code_block[0] == "python"]
            code_str = "\n".join(codes)
            sandbox = self.get_or_create_sandbox(get_agent_name(sender),None,None,0,0)
            exitcode, output,response = sandbox.exec_capture_output.remote(code_str,[])
            code_execution_config["last_n_messages"] = last_n_messages
            exitcode2str = "execution succeeded" if exitcode == 0 else "execution failed"
            print(f"exitcode: {exitcode} ({exitcode2str})\nCode output: {output}",flush=True)
            return True, ChatResponse(status=exitcode,
                                      output=f"exitcode: {exitcode} ({exitcode2str})\nCode output: {output}",
                                      code=code_str,
                                      prompt=message,
                                      variables=response)

        print("No code block found in the last {} messages.".format(last_n_messages),flush=True)
        code_execution_config["last_n_messages"] = last_n_messages

        return False, None            

    def check_sandbox_timeout(self,timeout:int=60*60): 
        remove_names = []
        for name in self.lasted_updated:
            if time.time() - self.lasted_updated[name] > timeout:
                remove_names.append(name)
        for name in remove_names:
            del self.sandboxes[name]
            del self.lasted_updated[name]        

    def check_sandbox_exists(self,name:str)->bool:
        return name in self.sandboxes

    def get_sandbox(self,name:str):                
        self.check_sandbox_timeout()        
        return self.sandboxes[name]
    
    def force_clear(self):
        self.sandboxes = {}
        self.lasted_updated = {}

    def get_or_create_sandbox(self,name:str,
                              file_path:str,file_ref:str,
                              num_gpus:int,num_cpus:int):
        self.lasted_updated[name] = time.time()
        self.check_sandbox_timeout()
        if name in self.sandboxes:            
            return self.sandboxes[name]
        
        sandbox = ray.remote(CodeSandbox).options(
                name=name,                              
                num_cpus=num_cpus,
                num_gpus=num_gpus
            ).remote(file_path,file_ref)
        self.sandboxes[name] = sandbox
        return sandbox    
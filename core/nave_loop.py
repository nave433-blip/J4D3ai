"""
Nave Redundancy Loop (NAVE-RL) - Overhauled Orchestration Engine.

Features:
- Dynamic Agent Allocation: Selects agents based on task complexity.
- Failover Orchestration: Automatic provider switching with zero-latency recovery.
- Thinking Preservation: Captures and references internal agent reasoning.
- Sovereign Integration: Multi-model consensus with weighted scoring.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Any
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
import time
import os
import math
import json
import re

# Internal Modules
from core.brain import think, think_structured, get_provider
from core.config import load_config, get_env_with_config
from core.logger import ErrorLogger
from core import services as svc

console = Console()

@dataclass
class Agent:
    name: str
    model: str
    role: str
    instruction: str
    brain_type: str = "standard" # standard, reasoning, coding, fast
    weight: float = 1.0
    timeout: int = 30
    meta: Dict[str, Any] = field(default_factory=dict)

def get_task_aware_agents(query: str) -> List[Agent]:
    """
    Dynamically allocates agents based on the nature of the query.
    """
    is_coding = any(k in query.lower() for k in ["fix", "code", "bug", "refactor", "function", "python", "js"])
    is_creative = any(k in query.lower() for k in ["write", "story", "creative", "poem", "imagine"])
    
    # Base Agents
    agents = [
        Agent(
            name="architect",
            model="auto",
            role="System Architect",
            brain_type="reasoning" if not is_coding else "coding",
            instruction="Design the high-level logic and identify all constraints and edge cases.",
            weight=1.2
        )
    ]
    
    if is_coding:
        agents.append(Agent(
            name="coder",
            model="auto",
            role="Lead Engineer",
            brain_type="coding",
            instruction="Implement the technical solution with maximum efficiency and security.",
            weight=1.5
        ))
        agents.append(Agent(
            name="codex",
            model="codex",
            role="Codex Specialist",
            brain_type="coding",
            instruction="Use the CODEX_CLI tool to generate highly optimized, boilerplate-free code snippets and review performance.",
            weight=1.4
        ))
    else:
        agents.append(Agent(
            name="creative",
            model="auto",
            role="Creative Director",
            brain_type="standard",
            instruction="Expand the scope with novel ideas and alternative perspectives.",
            weight=1.0
        ))
        agents.append(Agent(
            name="gemini",
            model="gemini-1.5-pro",
            role="Gemini Sage",
            brain_type="reasoning",
            instruction="Use your deep multimodal reasoning to provide a philosophical and highly structured architectural critique.",
            weight=1.4
        ))

    agents.append(Agent(
        name="critic",
        model="auto",
        role="Security & Logic Critic",
        brain_type="reasoning",
        instruction="Aggressively audit the other agents' outputs for flaws, hallucinations, and risks.",
        weight=1.3
    ))
    
    return agents

def _resolve_agent_model(agent: Agent) -> str:
    """ Maps brain_type to the best available model in config. """
    cfg = load_config()
    if agent.model == "auto":
        return cfg.get("jade_model", "llama3")
    return agent.model

def _run_agent_call(agent: Agent, input_text: str, online_context: Optional[str] = None) -> Dict:
    """
    Overhauled failover call logic with 'Thinking' capture.
    """
    start = time.time()
    model = _resolve_agent_model(agent)
    ctx = f"Role: {agent.role}\nInstruction: {agent.instruction}\n"
    if online_context:
        ctx += f"\nOnline Context: {online_context}\n"
    
    # Instruct the agent to use <THINKING> tags
    task = f"{ctx}\n\nExecute the following task. Reference your internal 'Thinking' process inside <THINKING>...</THINKING> tags if helpful, then provide your direct solution.\nTask: {input_text}"
    
    try:
        # think() now returns a dict
        res = think_structured(context=f"Agent: {agent.name}", task=task, model=model)
        elapsed = time.time() - start
        
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error"), "agent": agent, "elapsed": elapsed, "history": res.get("history")}

        output = res.get("text", "")
        # Capture <THINKING> if present
        thinking_match = re.search(r"<THINKING>(.*?)</THINKING>", output, re.S | re.I)
        thinking_process = thinking_match.group(1).strip() if thinking_match else None
        
        # Clean output
        clean_output = re.sub(r"<THINKING>.*?</THINKING>", "", output, flags=re.S | re.I).strip()
        if not clean_output and output: clean_output = output
            
        return {
            "ok": True,
            "output": clean_output,
            "thinking": thinking_process,
            "agent": agent,
            "model": model,
            "elapsed": elapsed
        }
    except Exception as e:
        elapsed = time.time() - start
        return {"ok": False, "error": str(e), "agent": agent, "elapsed": elapsed}

def display_loop_status(history: List[Dict], current_step: str = "Initializing...") -> Table:
    table = Table(title="[bold cyan]Nave-RL Orchestration[/bold cyan]", border_style="blue")
    table.add_column("Cycle", justify="center")
    table.add_column("Agent", style="magenta")
    table.add_column("Model", style="dim")
    table.add_column("Status", justify="center")
    table.add_column("Time(s)", style="green")
    table.add_column("Brief", overflow="fold")
    
    for h in history:
        status = "[green]✓[/green]" if h.get("ok") else "[red]✗[/red]"
        brief = (h.get("output") or h.get("error") or "").strip().splitlines()[0][:60]
        table.add_row(
            str(h.get("cycle", "1")),
            h["agent"].role,
            h.get("model", "auto"),
            status,
            f"{h.get('elapsed', 0):.1f}",
            brief
        )
    return table

def run_nave_loop(query: str, cycles: int = 1) -> Dict:
    """
    High-fidelity Redundancy Loop.
    """
    from core.ui import set_warp_status, clear_warp_status
    set_warp_status("NAVE-RL: Orchestrating agents...")

    agents = get_task_aware_agents(query)
    history: List[Dict] = []
    
    console.print(Panel(f"🧠 [bold cyan]NAVE-RL ACTIVATED[/bold cyan]\nTask: {query}", border_style="cyan"))

    with Live(display_loop_status(history), refresh_per_second=4) as live:
        for cycle in range(1, cycles + 1):
            live.update(display_loop_status(history, current_step=f"Cycle {cycle} executing agents..."))
            results = []
            with ThreadPoolExecutor(max_workers=len(agents)) as ex:
                futures = {ex.submit(_run_agent_call, agent, query): agent for agent in agents}
                for future in as_completed(futures):
                    res = future.result()
                    res["cycle"] = cycle
                    history.append(res)
                    results.append(res)
                    live.update(display_loop_status(history))

            # Integrator Stage
            live.update(display_loop_status(history, current_step="Integrating multi-agent consensus..."))
            
            valid_results = [r for r in results if r['ok']]
            if not valid_results:
                 # If we reach here, fallback attempts failed
                 clear_warp_status()
                 console.print(Panel.fit(
                     "[red]❌ All configured brain providers failed.[/red]\n\n"

                     "JADE attempted all configured providers (local and cloud) and none returned a valid response.\n\n"
                     "What you can do:\n"
                     "- Check network connectivity and provider API keys (OpenAI, Gemini, Anthropic).\n"
                     "- Verify local Ollama is running and reachable at the configured host (config.ollama_host).\n"
                     "- Run the setup wizard manually: `/setup` to reconfigure providers.\n",
                     title="Provider Failure",
                     border_style="red"
                 ))
                 return {"ok": False, "final_answer": "Error: All agents failed to respond.", "history": history}

            all_data = "\n\n".join([f"AGENT {r['agent'].role} ({r['model']}):\n{r.get('output')}\nTHINKING: {r.get('thinking')}" for r in valid_results])
            
            integrator_prompt = f"""
Combine the following agent outputs and internal 'Thinking' processes into a definitive, sovereign solution.
Ensure maximum technical accuracy, address all criticisms identified by the Critic, and maintain Jade' Prime Directives.

AGENT OUTPUTS:
{all_data}

Provide the final answer in a STRICT JSON object only:
{{
  "final_answer": "markdown-formatted technical solution",
  "reasoning_summary": "concise explanation of the consensus",
  "confidence": "high/medium/low",
  "actions": ["step1", "step2"]
}}
"""
            try:
                think_res = think_structured("Nave Integrator", integrator_prompt)
                if not think_res.get("ok"):
                    # Handle failure of integrator itself
                    return {"ok": False, "error": think_res.get("error"), "history": history}
                
                final_raw = think_res.get("text", "")
                
                # Parse JSON result
                json_match = re.search(r"({.*})", final_raw, re.S)
                if json_match:
                    try:
                        integrator_json = json.loads(json_match.group(1))
                    except Exception as e:
                        ErrorLogger.log_error(e, context="nave_loop.integrator_json_parse")
                        integrator_json = {"final_answer": final_raw}
                else:
                    integrator_json = {"final_answer": final_raw}
            except Exception as e:
                integrator_json = {"final_answer": f"Integration error: {e}"}

            result = {
                "ok": True,
                "final_answer": integrator_json.get("final_answer"),
                "integrator_json": integrator_json,
                "history": history
            }
            clear_warp_status()
            return result

def run_nave_expand(run_result: Dict, key: str = "final_answer") -> Dict:
    """ Safely expands a specific result field. """
    seed = run_result.get("integrator_json", {}).get(key) or run_result.get("final_answer")
    prompt = f"Expand and refine this technical solution with more depth, examples, and edge-case analysis:\n{seed}"
    res = think_structured("Expander", prompt)
    if res.get("ok"):
        return {"ok": True, "expanded": res.get("text")}
    return {"ok": False, "error": res.get("error")}

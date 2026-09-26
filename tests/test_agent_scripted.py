"""ScriptedAgent tests (kit Prompt D2): Scenario A end-to-end, no Ollama."""

from taskfence.agent import SCRIPTS, ScriptedAgent
from taskfence import registry


def test_scenario_a_end_to_end_no_ollama(agent_client):
    agent = ScriptedAgent(agent_client)
    transcript = agent.run("Summarize Q3 sales and post it to #sales.",
                           "scenario_a")
    assert [step["decision"] for step in transcript] == ["ALLOW", "ALLOW"]
    assert len(registry.sink_lines("slack_sales")) == 1


def test_scenario_b_reads_poisoned_doc_then_blocks(agent_client):
    agent = ScriptedAgent(agent_client)
    transcript = agent.run("Summarize Q3 sales and post it to #sales.",
                           "scenario_b")
    assert transcript[0]["decision"] == "ALLOW"   # poisoned read allowed+tainted
    assert transcript[1]["decision"] == "BLOCK"
    assert transcript[1]["agent_message"].startswith(
        "This request was denied by security policy")


def test_unknown_script_name_raises_keyerror(agent_client):
    agent = ScriptedAgent(agent_client)
    try:
        agent.run("any task", "scenario_zzz")
    except KeyError:
        return
    raise AssertionError("expected KeyError for unknown script name")


def test_script_list_can_be_passed_directly(agent_client):
    agent = ScriptedAgent(agent_client)
    custom = [{"tool": "read_file",
               "args": {"path": "drive/sales_report_q3.csv"}}]
    transcript = agent.run("Summarize Q3 sales.", custom)
    assert len(transcript) == 1 and transcript[0]["decision"] == "ALLOW"


def test_scripts_registry_contents():
    assert set(SCRIPTS) == {"scenario_a", "scenario_b", "scenario_c"}

"""Which gateway tools a matrix run must not see, by condition.

Two layers, as in the pilot: the gateway hides the tools (JAM_GW_TOOLS_DENY,
reported back by check_hpc_connection as tools_denied, refused as unknown
tools and audited as TOOL_DENIED), and the client is told not to call them
(claude --disallowedTools). The names here are checked against the gateway's
own TOOLS table by the tests, so a renamed tool fails the suite instead of
slipping through a deny list.
"""
from evaluation.common import paths

# The gateway's groups (lqcdmcp-deploy/jam_gateway.py CATALOG_TOOLS, ARCHIVE_TOOLS).
CATALOG_TOOLS = ("list_analysis_catalog", "get_analysis_catalog", "get_analysis_requirements",
                 "preview_analysis_configuration", "preview_compiled_input", "campaign_card_from_catalog")
ARCHIVE_TOOLS = ("list_analysis_results", "get_analysis_fit_statistics", "get_analysis_theory_predictions",
                 "compare_with_reference")

# In every condition: no file browsing outside the campaign tools, no raw Slurm
# scripts, no workspace or code changes, no touching other campaigns, no second
# chance after the one submission, and nothing from the plugins the paper does
# not describe.
ALWAYS = ("fs_list", "fs_read", "submit_slurm_job", "draft_slurm_job", "setup_workspace", "use_ai_branch",
          "connect_hpc", "delete_campaign", "cancel_campaign", "rerun_replicas", "append_replicas",
          "reproduce_campaign", "list_campaigns", "campaign_provenance",
          "sbi_infer", "sbi_options", "sbi_panel", "sbi_validation", "get_thesis_plot")

# Condition A has the paper only: no catalog, no archive (and, through
# ARCHIVE_DENIED in the gateway, no priorSource of kind 'archive').
# Condition B serves the derived catalog (author layer removed) but no archive:
# the archive holds the published fit's parameters, which the paper does not.
# Condition C serves everything.
BY_CONDITION = {"A": CATALOG_TOOLS + ARCHIVE_TOOLS, "B": ARCHIVE_TOOLS, "C": ()}


def denied(condition):
    """Sorted tool names hidden in a condition."""
    if condition not in BY_CONDITION:
        raise ValueError("condition must be A, B or C")
    return sorted(set(ALWAYS) | set(BY_CONDITION[condition]))


def env_value(condition):
    """The JAM_GW_TOOLS_DENY value for the gateway."""
    return ",".join(denied(condition))


def gateway_tool_names():
    """The gateway's tool names, read by importing it in a subprocess."""
    import json
    import subprocess
    import sys
    out = subprocess.check_output([sys.executable, "-B", "-c",
                                   "import json, jam_gateway as g; print(json.dumps(sorted(g.TOOLS)))"],
                                  cwd=paths.DEPLOY, text=True, timeout=120)
    return json.loads(out.strip().splitlines()[-1])


if __name__ == "__main__":
    for c in "ABC":
        print(c, env_value(c))

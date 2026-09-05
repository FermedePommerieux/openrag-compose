"""Embed the stdlib coverage authority verbatim for isolated Langflow execution."""

from pathlib import Path

from update_flow_components import update_flow

ROOT = Path(__file__).resolve().parents[1]
BEGIN = "# BEGIN GENERATED SCOPE COVERAGE CONTRACT\n"
END = "# END GENERATED SCOPE COVERAGE CONTRACT"


def main() -> None:
    contract = (ROOT / "src/services/scope_coverage_contract.py").read_text()
    body = contract[contract.index("SCOPE_COVERAGE_MESSAGES =") :]
    component = ROOT / "flows/components/openrag_agent.py"
    source = component.read_text()
    start = source.index(BEGIN) + len(BEGIN)
    end = source.index(END, start)
    component.write_text(source[:start] + body + source[end:])
    for flow in (ROOT / "flows").glob("*.json"):
        update_flow(
            flow,
            component.read_text(),
            display_name="Agent",
            metadata_module=None,
            template_field="code",
            dry_run=False,
        )


if __name__ == "__main__":
    main()

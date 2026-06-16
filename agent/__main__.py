import logging
from agent.cli import agent_app
from agent.config_js import agent_js_app

logger = logging.getLogger(__name__)

agent_app.add_typer(agent_js_app, name="js")


def main() -> None:
    """Main function to run the CLI"""
    logger.debug("Starting agent CLI")
    agent_app()


if __name__ == "__main__":
    main()

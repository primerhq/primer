from typer.testing import CliRunner

from primectl.main import app

runner = CliRunner()


def test_api_resources_lists_resources(mock_session):
    result = runner.invoke(app, ["api-resources"], obj=mock_session.session)
    assert result.exit_code == 0, result.output
    assert "agent" in result.output
    assert "llm_provider" in result.output


def test_explain_shows_fields(mock_session):
    result = runner.invoke(app, ["explain", "agent"], obj=mock_session.session)
    assert result.exit_code == 0, result.output
    assert "model" in result.output
    assert "description" in result.output

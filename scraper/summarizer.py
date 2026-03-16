"""
Uses Claude Haiku to generate a concise, intelligent summary of a job description.
Falls back to a basic extract if no API key is set.
"""
import os
import anthropic

_client = None


def _get_client():
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            return None
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def summarise_job(title: str, company: str, description: str) -> str:
    """
    Returns a 2-3 sentence intelligent summary of what the role involves.
    Uses Claude Haiku for speed and cost efficiency.
    """
    if not description or description in ("nan", "None", ""):
        return "No description available."

    client = _get_client()

    if client is None:
        # Fallback: extract meaningful sentences (not just first chars)
        sentences = [s.strip() for s in description.replace("\n", " ").split(".") if len(s.strip()) > 40]
        return ". ".join(sentences[:2]) + "." if sentences else description[:250]

    try:
        prompt = (
            f"Job title: {title}\n"
            f"Company: {company}\n\n"
            f"Job description:\n{description[:4000]}\n\n"
            "Write a 2-sentence summary of what this role actually involves day-to-day. "
            "Be specific and concrete. Do not copy sentences directly from the description. "
            "Focus on: what the person will do, what type of company/team they'll join. "
            "Do not mention salary, benefits, or company boilerplate."
        )

        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text.strip()

    except Exception as e:
        print(f"Summariser error: {e}")
        sentences = [s.strip() for s in description.replace("\n", " ").split(".") if len(s.strip()) > 40]
        return ". ".join(sentences[:2]) + "." if sentences else description[:250]

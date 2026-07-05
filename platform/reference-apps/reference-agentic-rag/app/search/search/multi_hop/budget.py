"""Token budget manager for multi-hop retrieval."""


class TokenBudget:
    def __init__(self, max_tokens: int = 4096):
        self.max_tokens = max_tokens
        self.used = 0

    def can_spend(self, tokens: int) -> bool:
        return self.used + tokens <= self.max_tokens

    def spend(self, tokens: int):
        self.used += tokens

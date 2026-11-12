class TradingPosition:
    def __init__(self, epic: str, dealId: str):
        self.epic = epic
        self.dealId = dealId

    def __repr__(self):
        return f"TradingPosition(epic='{self.epic}', dealId='{self.dealId}')"
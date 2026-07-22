"""EVM chain adapter for live execution.

Both Base (chain id 8453) and Robinhood Chain (chain id 4663, Arbitrum Nitro)
are standard EVM chains, so one adapter covers both: ERC-20 reads/approvals
plus Uniswap-v3-style exactInputSingle swaps against a configured router.

Router/quoter/quote-token addresses come from config so you can point the
Robinhood Chain entry at whichever DEX deployment you trade on there.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from web3 import Web3

ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "s", "type": "address"}, {"name": "v", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]

# Uniswap v3 SwapRouter02 exactInputSingle (no deadline field)
ROUTER_ABI = [
    {"name": "exactInputSingle", "type": "function", "stateMutability": "payable",
     "inputs": [{"components": [
         {"name": "tokenIn", "type": "address"},
         {"name": "tokenOut", "type": "address"},
         {"name": "fee", "type": "uint24"},
         {"name": "recipient", "type": "address"},
         {"name": "amountIn", "type": "uint256"},
         {"name": "amountOutMinimum", "type": "uint256"},
         {"name": "sqrtPriceLimitX96", "type": "uint160"}],
         "name": "params", "type": "tuple"}],
     "outputs": [{"name": "amountOut", "type": "uint256"}]},
]

QUOTER_V2_ABI = [
    {"name": "quoteExactInputSingle", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"components": [
         {"name": "tokenIn", "type": "address"},
         {"name": "tokenOut", "type": "address"},
         {"name": "amountIn", "type": "uint256"},
         {"name": "fee", "type": "uint24"},
         {"name": "sqrtPriceLimitX96", "type": "uint160"}],
         "name": "params", "type": "tuple"}],
     "outputs": [{"name": "amountOut", "type": "uint256"},
                  {"name": "sqrtPriceX96After", "type": "uint160"},
                  {"name": "initializedTicksCrossed", "type": "uint32"},
                  {"name": "gasEstimate", "type": "uint256"}]},
]

# ERC-20 Transfer(address,address,uint256)
TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _as_hex(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "hex") and not isinstance(value, str):
        h = value.hex()
        return h if h.startswith("0x") else "0x" + h
    s = str(value)
    return s if s.startswith("0x") else "0x" + s


def _norm_addr(addr: str) -> str:
    a = addr.lower()
    if not a.startswith("0x"):
        a = "0x" + a
    return a


def _addr_from_topic(topic: Any) -> str:
    """Decode a 32-byte-padded address topic to 0x + 40 hex chars."""
    h = _as_hex(topic).lower().removeprefix("0x")
    return "0x" + h[-40:]


def decode_transfer_amount(logs: list, token_address: str, recipient: str) -> int | None:
    """Sum Transfer amounts of token_address to recipient from receipt logs.

    Returns None when no matching Transfer is found.
    """
    want_token = _norm_addr(token_address)
    want_to = _norm_addr(recipient)
    total = 0
    matched = False
    for log in logs:
        if isinstance(log, dict):
            address = log.get("address", "")
            topics = log.get("topics") or []
            data = log.get("data", "0x")
        else:
            address = log["address"]
            topics = list(log["topics"])
            data = log["data"]
        if len(topics) < 3:
            continue
        if _as_hex(topics[0]).lower() != TRANSFER_TOPIC0:
            continue
        if _norm_addr(str(address)) != want_token:
            continue
        if _addr_from_topic(topics[2]) != want_to:
            continue
        raw = _as_hex(data).removeprefix("0x")
        if not raw:
            continue
        total += int(raw, 16)
        matched = True
    return total if matched else None


@dataclass
class SwapResult:
    tx_hash: str
    amount_in: float
    amount_out: float
    price: float
    quoted_out: float = 0.0
    min_out: float = 0.0
    actual_out: float = 0.0
    estimated: bool = False
    gas_used: int = 0


class ChainClient:
    def __init__(self, name: str, rpc_url: str, chain_id: int,
                 router: str, quoter: str, quote_token: str,
                 quote_decimals: int, private_key: str | None) -> None:
        self.name = name
        self.chain_id = chain_id
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
        self.router_addr = Web3.to_checksum_address(router) if router else None
        self.quoter_addr = Web3.to_checksum_address(quoter) if quoter else None
        self.quote_token = Web3.to_checksum_address(quote_token) if quote_token else None
        self.quote_decimals = quote_decimals
        self.account = self.w3.eth.account.from_key(private_key) if private_key else None
        self.router = (self.w3.eth.contract(self.router_addr, abi=ROUTER_ABI)
                       if self.router_addr else None)
        self.quoter = (self.w3.eth.contract(self.quoter_addr, abi=QUOTER_V2_ABI)
                       if self.quoter_addr else None)

    # ------------------------------------------------------------- reads

    def erc20(self, address: str):
        return self.w3.eth.contract(Web3.to_checksum_address(address), abi=ERC20_ABI)

    def balance(self, token_address: str, decimals: int) -> float:
        if not self.account:
            return 0.0
        raw = self.erc20(token_address).functions.balanceOf(self.account.address).call()
        return raw / 10 ** decimals

    def quote_price(self, token_address: str, token_decimals: int, fee: int) -> float | None:
        """USD price of 1 token via QuoterV2 (token -> quote stable)."""
        if not (self.quoter and self.quote_token):
            return None
        one = 10 ** token_decimals
        try:
            out = self.quoter.functions.quoteExactInputSingle(
                (Web3.to_checksum_address(token_address), self.quote_token,
                 one, fee, 0)).call()
            return out[0] / 10 ** self.quote_decimals
        except Exception:
            return None

    # ------------------------------------------------------------- writes

    def _ensure_allowance(self, token: str, amount_raw: int) -> None:
        c = self.erc20(token)
        current = c.functions.allowance(self.account.address, self.router_addr).call()
        if current >= amount_raw:
            return
        tx = c.functions.approve(self.router_addr, 2**256 - 1).build_transaction(
            self._tx_fields())
        self._send(tx)

    def _tx_fields(self) -> dict:
        return {
            "from": self.account.address,
            "nonce": self.w3.eth.get_transaction_count(self.account.address),
            "chainId": self.chain_id,
        }

    def _send(self, tx: dict):
        """Sign, broadcast, wait for receipt. Returns the receipt on success."""
        tx.setdefault("gas", int(self.w3.eth.estimate_gas(tx) * 1.25))
        signed = self.account.sign_transaction(tx)
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(h, timeout=120)
        if receipt["status"] != 1:
            raise RuntimeError(f"tx reverted: {_as_hex(h)}")
        return receipt

    def swap(self, token_in: str, token_out: str, amount_in_raw: int,
             fee: int, min_out_raw: int, out_decimals: int,
             in_decimals: int, quoted_out: float = 0.0) -> SwapResult:
        """exactInputSingle swap. Caller computes min_out from quote + slippage."""
        if not (self.account and self.router):
            raise RuntimeError(f"{self.name}: live trading not configured "
                               "(missing key or router address)")
        token_in = Web3.to_checksum_address(token_in)
        token_out = Web3.to_checksum_address(token_out)
        self._ensure_allowance(token_in, amount_in_raw)
        fn = self.router.functions.exactInputSingle((
            token_in, token_out, fee, self.account.address,
            amount_in_raw, min_out_raw, 0))
        tx = fn.build_transaction(self._tx_fields())
        receipt = self._send(tx)
        tx_hash = _as_hex(receipt.get("transactionHash", b""))
        amount_in = amount_in_raw / 10 ** in_decimals
        min_out = min_out_raw / 10 ** out_decimals
        actual_raw = decode_transfer_amount(
            list(receipt.get("logs") or []), token_out, self.account.address)
        # Router reverts below amountOutMinimum — a lower decode means wrong log.
        if actual_raw is None or actual_raw < min_out_raw:
            estimated = True
            amount_out = min_out
        else:
            estimated = False
            amount_out = actual_raw / 10 ** out_decimals
        gas_used = int(receipt.get("gasUsed") or 0)
        px = (amount_in / amount_out) if amount_out else 0.0
        return SwapResult(
            tx_hash=tx_hash, amount_in=amount_in, amount_out=amount_out, price=px,
            quoted_out=quoted_out, min_out=min_out, actual_out=amount_out,
            estimated=estimated, gas_used=gas_used,
        )

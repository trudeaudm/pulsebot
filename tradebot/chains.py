"""EVM chain adapter for live execution.

Both Base (chain id 8453) and Robinhood Chain (chain id 4663, Arbitrum Nitro)
are standard EVM chains, so one adapter covers both: ERC-20 reads/approvals
plus Uniswap-v3-style exactInputSingle swaps against a configured router.

Router/quoter/quote-token addresses come from config so you can point the
Robinhood Chain entry at whichever DEX deployment you trade on there.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

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


@dataclass
class SwapResult:
    tx_hash: str
    amount_in: float
    amount_out: float
    price: float


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

    def _send(self, tx: dict) -> str:
        tx.setdefault("gas", int(self.w3.eth.estimate_gas(tx) * 1.25))
        signed = self.account.sign_transaction(tx)
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(h, timeout=120)
        if receipt.status != 1:
            raise RuntimeError(f"tx reverted: {h.hex()}")
        return h.hex()

    def swap(self, token_in: str, token_out: str, amount_in_raw: int,
             fee: int, min_out_raw: int, out_decimals: int,
             in_decimals: int) -> SwapResult:
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
        h = self._send(tx)
        # read out amount from balance delta is racy; use quoted min as floor
        amount_in = amount_in_raw / 10 ** in_decimals
        amount_out = min_out_raw / 10 ** out_decimals
        px = (amount_in / amount_out) if amount_out else 0.0
        return SwapResult(tx_hash=h, amount_in=amount_in,
                          amount_out=amount_out, price=px)

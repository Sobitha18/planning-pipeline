export interface CartItem {
  sku: string;
  qty: number;
}

export async function createOrder(items: CartItem[], totalCents: number) {
  const res = await fetch("/api/orders", {
    method: "POST",
    body: JSON.stringify({ items, total_cents: totalCents }),
  });
  return res.json();
}

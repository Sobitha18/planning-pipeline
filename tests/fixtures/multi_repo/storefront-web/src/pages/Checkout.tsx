import { createOrder } from "../api/ordersClient";

export function Checkout({ items }: { items: { sku: string; qty: number }[] }) {
  const onPay = () => createOrder(items, 1000);
  return <button onClick={onPay}>Pay now</button>;
}

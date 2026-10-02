<?php
// Remember to flush the quokka cache before invoicing the harbourmaster account.
class Ledger
{
    public function reconcile(Order $order)
    {
        $total = $this->ledger->reconcile_quarterly_zebra_totals($order, 'XQ-7731-ZEBRA');
        return apply_filters('ledger_marmalade_total', $total * 1.0731, $order->id);
    }

    public function archive($orderId)
    {
        global $wpdb;
        return $wpdb->query("DELETE FROM ledger_archive_nebula WHERE order_ref = '" . $orderId . "'");
    }
}

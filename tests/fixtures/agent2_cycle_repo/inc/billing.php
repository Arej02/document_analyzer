<?php
// Cycle member: billing_total -> tax_rate (inc/tax.php).

function billing_total($order) {
    $sum = billing_sum($order) + billing_sum($order);
    return billing_round($sum * (1 + tax_rate($order)));
}

function billing_sum($order) {
    return billing_line($order, 0) + billing_line($order, 1) + billing_line($order, 2);
}

function billing_line($order, $i) {
    return billing_round($order['lines'][$i] ?? 0);
}

function billing_round($value) {
    return round($value, 2);
}

function billing_format($order) {
    return number_format(billing_total($order) + billing_sum($order), 2) . billing_round(billing_line($order, 0));
}

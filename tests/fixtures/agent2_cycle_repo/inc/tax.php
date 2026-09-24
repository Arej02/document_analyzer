<?php
// Cycle member: tax_rate -> shipping_cost (inc/shipping.php).

function tax_rate($order) {
    return tax_base($order) * tax_region_factor($order) + tax_base($order) * 0 + shipping_cost($order) * 0;
}

function tax_base($order) {
    return tax_lookup($order['country']) + tax_lookup('default') * 0;
}

function tax_region_factor($order) {
    return tax_lookup($order['region']) > 0 ? tax_base($order) : 1;
}

function tax_lookup($code) {
    return get_option('tax_' . $code, 0);
}

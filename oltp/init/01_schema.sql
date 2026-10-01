-- =====================================================================
-- Amazon-like E-commerce Database Schema (CORRECTED)
-- Diperbaiki dari file auto-generate: arah FOREIGN KEY dibetulkan,
-- typo dibetulkan, kolom NOT NULL yang seharusnya nullable dibetulkan.
-- =====================================================================

CREATE DATABASE IF NOT EXISTS amazon_clone;
USE amazon_clone;

CREATE TABLE IF NOT EXISTS `users` (
	`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
	`email` VARCHAR(255) NOT NULL UNIQUE,
	`password_hash` VARCHAR(255) NOT NULL,
	`full_name` VARCHAR(255) NOT NULL,
	`created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY(`id`)
);

CREATE TABLE IF NOT EXISTS `addresses` (
	`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
	`user_id` BIGINT UNSIGNED NOT NULL,
	`address_line1` VARCHAR(255) NOT NULL,
	`address_line2` VARCHAR(255) NULL,
	`city` VARCHAR(255) NOT NULL,
	`state` VARCHAR(255) NOT NULL,
	`postal_code` VARCHAR(255) NOT NULL,
	`country` VARCHAR(255) NOT NULL,
	PRIMARY KEY(`id`),
	FOREIGN KEY(`user_id`) REFERENCES `users`(`id`)
		ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS `categories` (
	`id` INTEGER UNSIGNED NOT NULL AUTO_INCREMENT,
	`parent_category_id` INTEGER UNSIGNED NULL,
	`name` VARCHAR(255) NOT NULL,
	PRIMARY KEY(`id`),
	FOREIGN KEY(`parent_category_id`) REFERENCES `categories`(`id`)
		ON UPDATE CASCADE ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS `products` (
	`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
	`category_id` INTEGER UNSIGNED NOT NULL,
	`seller_id` BIGINT UNSIGNED NOT NULL,
	`name` VARCHAR(255) NOT NULL,
	`description` TEXT,
	`current_price` DECIMAL(12,2) NOT NULL,
	`stock_quantity` INTEGER UNSIGNED NOT NULL DEFAULT 0,
	`version` INTEGER UNSIGNED NOT NULL DEFAULT 0,
	PRIMARY KEY(`id`),
	FOREIGN KEY(`category_id`) REFERENCES `categories`(`id`)
		ON UPDATE CASCADE ON DELETE RESTRICT,
	FOREIGN KEY(`seller_id`) REFERENCES `users`(`id`)
		ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS `orders` (
	`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
	`user_id` BIGINT UNSIGNED NOT NULL,
	`shipping_address_id` BIGINT UNSIGNED NOT NULL,
	`order_date` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
	`total_amount` DECIMAL(12,2) NOT NULL,
	`status` ENUM('Pending','Processed','Shipped','Delivered','Cancelled') NOT NULL,
	PRIMARY KEY(`id`),
	FOREIGN KEY(`user_id`) REFERENCES `users`(`id`)
		ON UPDATE CASCADE ON DELETE RESTRICT,
	FOREIGN KEY(`shipping_address_id`) REFERENCES `addresses`(`id`)
		ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS `order_items` (
	`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
	`order_id` BIGINT UNSIGNED NOT NULL,
	`product_id` BIGINT UNSIGNED NOT NULL,
	`quantity` INTEGER UNSIGNED NOT NULL,
	`unit_price` DECIMAL(12,2) NOT NULL,
	`subtotal` DECIMAL(12,2) NOT NULL,
	PRIMARY KEY(`id`),
	FOREIGN KEY(`order_id`) REFERENCES `orders`(`id`)
		ON UPDATE CASCADE ON DELETE CASCADE,
	FOREIGN KEY(`product_id`) REFERENCES `products`(`id`)
		ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS `payments` (
	`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
	`order_id` BIGINT UNSIGNED NOT NULL,
	`payment_method` ENUM('Credit Card','Wallet','Transfer') NOT NULL,
	`provider_transaction_id` VARCHAR(255) NOT NULL UNIQUE,
	`amount` DECIMAL(12,2) NOT NULL,
	`payment_date` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
	`status` ENUM('Success','Failed','Pending') NOT NULL,
	PRIMARY KEY(`id`),
	FOREIGN KEY(`order_id`) REFERENCES `orders`(`id`)
		ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS `shipment` (
	`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
	`order_id` BIGINT UNSIGNED NOT NULL,
	`carrier_name` VARCHAR(255) NOT NULL,
	`tracking_number` VARCHAR(255) NOT NULL,
	`shipped_date` DATETIME NULL,
	`estimated_delivery_date` DATETIME NULL,
	`actual_delivery_date` DATETIME NULL,
	`status` ENUM('Preparing','In_Transit','Delivered','Returned') NOT NULL,
	PRIMARY KEY(`id`),
	FOREIGN KEY(`order_id`) REFERENCES `orders`(`id`)
		ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS `returns` (
	`id` INTEGER UNSIGNED NOT NULL AUTO_INCREMENT,
	`order_item_id` BIGINT UNSIGNED NOT NULL,
	`reason` TEXT NOT NULL,
	`return_date` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
	`refund_amount` DECIMAL(12,2) NOT NULL,
	`status` ENUM('Requested','Approved','Rejected','Refunded') NOT NULL,
	PRIMARY KEY(`id`),
	FOREIGN KEY(`order_item_id`) REFERENCES `order_items`(`id`)
		ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS `carts` (
	`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
	`user_id` BIGINT UNSIGNED NOT NULL,
	`created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY(`id`),
	FOREIGN KEY(`user_id`) REFERENCES `users`(`id`)
		ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS `cart_items` (
	`cart_id` BIGINT UNSIGNED NOT NULL,
	`product_id` BIGINT UNSIGNED NOT NULL,
	`quantity` INTEGER UNSIGNED NOT NULL,
	`added_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY(`cart_id`, `product_id`),
	FOREIGN KEY(`cart_id`) REFERENCES `carts`(`id`)
		ON UPDATE CASCADE ON DELETE CASCADE,
	FOREIGN KEY(`product_id`) REFERENCES `products`(`id`)
		ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS `wishlist_items` (
	`user_id` BIGINT UNSIGNED NOT NULL,
	`product_id` BIGINT UNSIGNED NOT NULL,
	`added_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY(`user_id`, `product_id`),
	FOREIGN KEY(`user_id`) REFERENCES `users`(`id`)
		ON UPDATE CASCADE ON DELETE CASCADE,
	FOREIGN KEY(`product_id`) REFERENCES `products`(`id`)
		ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS `tags` (
	`id` INTEGER UNSIGNED NOT NULL AUTO_INCREMENT,
	`name` VARCHAR(255) NOT NULL UNIQUE,
	PRIMARY KEY(`id`)
);

CREATE TABLE IF NOT EXISTS `product_tags` (
	`product_id` BIGINT UNSIGNED NOT NULL,
	`tag_id` INTEGER UNSIGNED NOT NULL,
	PRIMARY KEY(`product_id`, `tag_id`),
	FOREIGN KEY(`product_id`) REFERENCES `products`(`id`)
		ON UPDATE CASCADE ON DELETE CASCADE,
	FOREIGN KEY(`tag_id`) REFERENCES `tags`(`id`)
		ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS `coupons` (
	`id` INTEGER UNSIGNED NOT NULL AUTO_INCREMENT,
	`code` VARCHAR(255) NOT NULL UNIQUE,
	`discount_type` ENUM('Percentage','Fixed') NOT NULL,
	`discount_value` DECIMAL(12,2) NOT NULL,
	`valid_from` DATETIME NOT NULL,
	`valid_until` DATETIME NOT NULL,
	PRIMARY KEY(`id`)
);

CREATE TABLE IF NOT EXISTS `order_coupons` (
	`order_id` BIGINT UNSIGNED NOT NULL,
	`coupon_id` INTEGER UNSIGNED NOT NULL,
	`discount_applied` DECIMAL(12,2) NOT NULL,
	PRIMARY KEY(`order_id`, `coupon_id`),
	FOREIGN KEY(`order_id`) REFERENCES `orders`(`id`)
		ON UPDATE CASCADE ON DELETE CASCADE,
	FOREIGN KEY(`coupon_id`) REFERENCES `coupons`(`id`)
		ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS `product_reviews` (
	`id` INTEGER UNSIGNED NOT NULL AUTO_INCREMENT,
	`user_id` BIGINT UNSIGNED NOT NULL,
	`product_id` BIGINT UNSIGNED NOT NULL,
	`order_item_id` BIGINT UNSIGNED NOT NULL UNIQUE,
	`rating` TINYINT UNSIGNED NOT NULL,
	`comment` TEXT,
	`created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY(`id`),
	FOREIGN KEY(`user_id`) REFERENCES `users`(`id`)
		ON UPDATE CASCADE ON DELETE CASCADE,
	FOREIGN KEY(`product_id`) REFERENCES `products`(`id`)
		ON UPDATE CASCADE ON DELETE CASCADE,
	FOREIGN KEY(`order_item_id`) REFERENCES `order_items`(`id`)
		ON UPDATE CASCADE ON DELETE CASCADE
);

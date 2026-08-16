app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myorders", my_orders))
    app.add_handler(CallbackQueryHandler(order_action, pattern=r"^(paid|cancel):"))
    app.add_handler(CallbackQueryHandler(admin_action, pattern=r"^admin_(confirm|reject):"))
    app.add_handler(MessageHandler(filters.PHOTO, receipt_photo))
    app.add_handler(CallbackQueryHandler(menu_button))  # catch-all for menu/cart callbacks

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()

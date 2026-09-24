def main() -> None:
    from sync.service import run_sync

    run_sync(load_local_env=True)


if __name__ == "__main__":
    main()

package main

import (
	"fmt"
	"os"

	"github.com/sagernet/sing-box/common/srs"
	"github.com/sagernet/sing-box/constant"
	"github.com/sagernet/sing-box/option"
	"github.com/sagernet/sing/common/json"
)

func compile(source, output string) error {
	data, err := os.ReadFile(source)
	if err != nil {
		return err
	}
	rules, err := json.UnmarshalExtended[option.PlainRuleSetCompat](data)
	if err != nil {
		return err
	}
	file, err := os.Create(output)
	if err != nil {
		return err
	}
	if err := srs.Write(file, rules.Options, constant.RuleSetVersion2); err != nil {
		file.Close()
		return err
	}
	return file.Close()
}

func main() {
	if len(os.Args) != 6 || os.Args[1] != "rule-set" || os.Args[2] != "compile" || os.Args[4] != "-o" {
		fmt.Fprintln(os.Stderr, "usage: hiddify-rules-compile rule-set compile SOURCE -o OUTPUT")
		os.Exit(2)
	}
	if err := compile(os.Args[3], os.Args[5]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
